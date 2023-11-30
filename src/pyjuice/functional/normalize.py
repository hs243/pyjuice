import torch
import triton
import triton.language as tl

from typing import Optional


@triton.jit
def _cum_params_kernel(params_ptr, cum_params_ptr, node_ids_ptr, num_param_blocks, group_size, batch_size, 
                       BLOCK_M: tl.constexpr, BLOCK_K: tl.constexpr, BLOCK_B: tl.constexpr):

    b_pid = tl.program_id(axis = 0)
    k_pid = tl.program_id(axis = 1)
    m_pid = tl.program_id(axis = 2)

    m_offsets = m_pid * BLOCK_M + tl.arange(0, BLOCK_M)
    m_mask = offsets < num_param_blocks

    group_size = k_pid * BLOCK_K + tl.arange(0, BLOCK_K)

    b_offsets = b_pid * BLOCK_B + tl.arange(0, BLOCK_B)
    b_mask = offsets < batch_size

    n_offsets = tl.load(node_ids_ptr + m_offsets, mask = m_mask, other = 0)
    reuse_offs = group_size[None,:,None] * batch_size + b_offsets[None,None,:]

    n_offsets = n_offsets[:,None,None] * (batch_size * group_size) + reuse_offs
    p_offsets = m_offsets[:,None,None] * reuse_offs

    mask = m_mask[:,None,None] & b_mask[None,None,:]
    params = tl.load(params_ptr + p_offsets, mask = mask, other = 0)

    tl.atomic_add(cum_params_ptr + n_offsets, params, mask = mask)


@triton.jit
def _norm_params_kernel(params_ptr, cum_params_ptr, node_ids_ptr, node_nchs_ptr, num_param_blocks, group_size, 
                        batch_size, pseudocount, BLOCK_M: tl.constexpr, BLOCK_K: tl.constexpr, BLOCK_B: tl.constexpr):

    b_pid = tl.program_id(axis = 0)
    k_pid = tl.program_id(axis = 1)
    m_pid = tl.program_id(axis = 2)

    m_offsets = m_pid * BLOCK_M + tl.arange(0, BLOCK_M)
    m_mask = offsets < num_param_blocks

    group_size = k_pid * BLOCK_K + tl.arange(0, BLOCK_K)

    b_offsets = b_pid * BLOCK_B + tl.arange(0, BLOCK_B)
    b_mask = offsets < batch_size

    n_offsets = tl.load(node_ids_ptr + m_offsets, mask = m_mask, other = 0)
    reuse_offs = group_size[None,:,None] * batch_size + b_offsets[None,None,:]

    nb_offsets = n_offsets[:,None,None] * (batch_size * group_size) + reuse_offs
    p_offsets = m_offsets[:,None,None] * reuse_offs

    mask = m_mask[:,None,None] & b_mask[None,None,:]
    params = tl.load(params_ptr + p_offsets, mask = mask, other = 0)
    cum_params = tl.load(cum_params_ptr + nb_offsets, mask = mask, other = 1)
    nchs = tl.load(node_nchs_ptr + n_offsets, mask = m_mask, other = 1)[:,None,None]
    
    normed_params = (params + pseudocount / nchs) / (cum_params + pseudocount)
    tl.store(params_ptr + p_offsets, normed_params, mask = mask)


def normalize_parameters(params: torch.Tensor, node_ids: torch.Tensor, group_size: int, ch_group_size: int, 
                         node_nchs: Optional[torch.Tensor] = None, pseudocount: float = 0.0):

    assert 3 <= params.dim() <= 4 and params.size(1) == group_size and params.size(2) == ch_group_size

    num_param_blocks = params.size(0)
    num_node_groups = torch.max(node_ids).detach().cpu().item() + 1

    if node_nchs is None:
        node_nchs = torch.bincount(node_ids) * ch_group_size

    if node_ids.is_cuda:
        assert params.is_cuda, "Input `params` should be on GPU."

        if params.dim() == 3:
            params = params.unsqueeze(3)

        batch_size = params.size(3)

        cum_params = torch.zeros([num_node_groups, group_size, batch_size], dtype = torch.float32, device = params.device)

        grouped_params = params.sum(2).contiguous()

        BLOCK_B = min(batch_size, 128)
        BLOCK_K = min(1024 // BLOCK_B, triton.next_power_of_2(group_size))
        BLOCK_M = min(1024 // (BLOCK_B * BLOCK_K), triton.next_power_of_2(num_param_blocks))

        grid = lambda meta: (triton.cdiv(batch_size, BLOCK_B), triton.cdiv(group_size, BLOCK_K), triton.cdiv(num_param_blocks, BLOCK_M))

        _cum_params_kernel[grid](grouped_params, cum_params, node_ids, num_param_blocks, group_size, batch_size, BLOCK_M, BLOCK_K, BLOCK_B)
        _norm_params_kernel[grid2](grouped_params, cum_params, node_ids, node_nchs, num_param_blocks, group_size, batch_size, pseudocount, BLOCK_M, BLOCK_K, BLOCK_B)

        params *= (grouped_params / params.sum(2)).unsqueeze(2)

    else:
        assert params.dim() == 3, "CPU version of `normalize_parameters` does not support `batch_size > 1` for now."

        with torch.no_grad():

            params = params.float()

            grouped_params = params.sum(dim = 2).contiguous()

            param_ids = torch.arange(0, num_param_blocks, dtype = torch.long, device = params.device)

            cum_matrix1 = torch.sparse_coo_tensor(
                torch.stack((node_ids, param_ids), dim = 0), 
                torch.ones([num_param_blocks], device = params.device), 
                (num_node_groups, num_param_blocks)
            )
            node_buffer = torch.sparse.mm(cum_matrix1, grouped_params) + pseudocount

            node_buffer.reciprocal_()
            node_buffer = node_buffer.reshape(num_node_groups * group_size, 1)

            param_ids = torch.arange(0, num_param_blocks * group_size, dtype = torch.long, device = params.device)

            cum_matrix2 = torch.sparse_coo_tensor(
                torch.stack((param_ids, node_ids.unsqueeze(1).repeat(1, group_size).reshape(-1)), dim = 0), 
                (grouped_params + pseudocount / node_nchs[node_ids].unsqueeze(1)).reshape(-1), (num_param_blocks * group_size, num_node_groups)
            )
            params_buffer = torch.sparse.mm(cum_matrix2, node_buffer).reshape(num_param_blocks, group_size)
            
            params *= (params_buffer / grouped_params).unsqueeze(2)