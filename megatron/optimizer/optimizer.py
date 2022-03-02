# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Megatron optimizer."""

from abc import ABC
from abc import abstractmethod

import torch

from apex.multi_tensor_apply import multi_tensor_applier
import amp_C

from megatron import get_timers
from megatron import mpu
from megatron import print_rank_0

from .clip_grads import clip_grad_norm_fp32, count_zeros_fp32

# >>>
from lutil import pax, tp

DEBUG_ITERATION = 2 # 10
# <<<


def _zero_grad_group_helper(group, set_to_none):
    """Zero out the gradient for a group of parameters.
    Note: copied from torch.optim.optimizer."""
    for param in group:
        if param.grad is not None:
            if set_to_none:
                param.grad = None
            else:
                if param.grad.grad_fn is not None:
                    param.grad.detach_()
                else:
                    param.grad.requires_grad_(False)
                param.grad.zero_()


def _multi_tensor_copy_this_to_that(this, that, overflow_buf=None):
    """Use multi-tensor-applier to copy values from one list to another.
    We don't have a blfoat16 implementation so for now if the overflow_buf
    is not provided, we default back to simple loop copy to be compatible
    with bfloat16."""
    if overflow_buf:
        overflow_buf.fill_(0)
        # Scaling with factor `1.0` is equivalent to copy.
        multi_tensor_applier(amp_C.multi_tensor_scale,
                             overflow_buf,
                             [this, that],
                             1.0)
    else:
        for this_, that_ in zip(this, that):
            that_.copy_(this_)



class MegatronOptimizer(ABC):


    def __init__(self, optimizer, clip_grad,
                 log_num_zeros_in_grad,
                 params_have_main_grad,
                 use_contiguous_buffers_in_local_ddp):

        """Input optimizer is the base optimizer for example Adam."""
        self.optimizer = optimizer
        assert self.optimizer, 'no optimizer is provided.'
        # Set gradient clipping and logging params.
        self.clip_grad = clip_grad
        self.log_num_zeros_in_grad = log_num_zeros_in_grad
        self.params_have_main_grad = params_have_main_grad
        self.use_contiguous_buffers_in_local_ddp = use_contiguous_buffers_in_local_ddp

        if self.use_contiguous_buffers_in_local_ddp:
            assert self.params_have_main_grad, \
                "use of contiguous buffer requires that params have main grad"

    def get_parameters(self):
        params = []
        for param_group in self.optimizer.param_groups:
            for param in param_group['params']:
                params.append(param)
        return params


    def clip_grad_norm(self, clip_grad, ITERATION):
        # >>>
        return
        # <<<
        params = self.get_parameters()
        return clip_grad_norm_fp32(params, clip_grad, ITERATION = ITERATION)


    def count_zeros(self):
        params = self.get_parameters()
        return count_zeros_fp32(params)


    @abstractmethod
    def zero_grad(self, set_to_none=True):
        pass


    @abstractmethod
    def get_loss_scale(self):
        """The output should be a cuda tensor of size 1."""
        pass


    def scale_loss(self, loss):
        """Simple scaling."""
        return self.get_loss_scale() * loss


    @abstractmethod
    def reduce_grads(self):
        pass


    @abstractmethod
    def step(self):
        pass


    @abstractmethod
    def gather_params(self):
        pass


    @abstractmethod
    def reload_model_params(self):
        """Refreshes any internal state from the current model parameters.
        Call whenever the parameters are changed outside of the optimizer.
        For example, when we load a model from a checkpoint  without loading
        the optimizer, the model parameters are updated but for fp16 optimizer
        with main parameters, the main parameters need to also be updated."""
        pass


    @abstractmethod
    def state_dict(self):
        pass


    @abstractmethod
    def load_state_dict(self, state_dict):
        pass


    # Promote state so it can be retrieved or set via
    # "optimizer_instance.state"
    def _get_state(self):
        return self.optimizer.state

    def _set_state(self, value):
        self.optimizer.state = value

    state = property(_get_state, _set_state)


    # Promote param_groups so it can be retrieved or set via
    # "optimizer_instance.param_groups"
    # (for example, to adjust the learning rate)
    def _get_param_groups(self):
        return self.optimizer.param_groups

    def _set_param_groups(self, value):
        self.optimizer.param_groups = value

    param_groups = property(_get_param_groups, _set_param_groups)


class BaseFloat16Optimizer(MegatronOptimizer):

    def __init__(self, optimizer, clip_grad, log_num_zeros_in_grad,
                 params_have_main_grad, use_contiguous_buffers_in_local_ddp,
                 bf16, grad_scaler,
                 models):

        super().__init__(
            optimizer, clip_grad, log_num_zeros_in_grad,
            params_have_main_grad, use_contiguous_buffers_in_local_ddp)

        # >>>
        self.models = models
        # <<<
        self.bf16 = bf16
        self.grad_scaler = grad_scaler
        # None grad scaler is only supported for bf16.
        if self.grad_scaler is None:
            assert self.bf16, 'fp16 expects a grad scaler.'

        # Tensor used to determine if a nan/if has happend.
        # Any non-zero value indicates inf/nan.
        # Note that we keep this for the cases that grad scaler is none.
        # We still record nan/inf if we have a bfloat16 with a grad scaler.
        if self.grad_scaler:
            self.found_inf = torch.cuda.FloatTensor([0.0])

        # Dummy tensor needed for apex multi-apply tensor.
        # For bfloat, we don't have multi-tensor apply and for now
        # we set it to none so the multi-tensor apply gets ignored.
        if bf16:
            self._dummy_overflow_buf = None
        else:
            self._dummy_overflow_buf = torch.cuda.IntTensor([0])

        # In case grad scaler is not passed, define the unity scale.
        if self.grad_scaler is None:
            self._scale_one = torch.cuda.FloatTensor([1.0])


    def get_loss_scale(self):
        if self.grad_scaler is None:
            return self._scale_one
        return self.grad_scaler.scale


    def reload_model_params(self):
        self._copy_model_params_to_main_params()


    def _unscale_main_grads_and_check_for_nan(self):

        # Collect main grads.
        main_grads = self._collect_main_grad_data_for_unscaling()
        # pax(1, {"main_grads": main_grads})

        # Reset found inf.
        self.found_inf.fill_(0.0)

        # Unscale and set found inf/nan
        torch._amp_foreach_non_finite_check_and_unscale_(
            main_grads, self.found_inf, self.grad_scaler.inv_scale)

        # Update across all model parallel instances.
        # >>>
        # torch.distributed.all_reduce(self.found_inf,
        #                              op=torch.distributed.ReduceOp.MAX,
        #                              group=mpu.get_model_parallel_group())
        # +++
        torch.distributed.all_reduce(self.found_inf,
                                     op=torch.distributed.ReduceOp.MAX)
        # <<<

        # Check for nan.
        found_inf_flag = (self.found_inf.item() > 0)

        return found_inf_flag

    # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
    @classmethod
    def debug_general(cls, ITERATION, key, value):
        from megatron import get_args
        args = get_args()
        my_rank = torch.distributed.get_rank()
        if ITERATION != DEBUG_ITERATION:
            return
        for r in range(torch.distributed.get_world_size()):
            if my_rank == r:
                print("            + br/%s; [r%d, i%d]; %s, %.12e" % ("fix " if args.use_distributed_optimizer else "main", my_rank, ITERATION, key, value))
            torch.distributed.barrier()
        torch.distributed.barrier()
        # if my_rank == 0:
        #     raise Exception("debug.")
        # else:
        #     exit(0)
        exit(0)

    # def _debug_model(self, ITERATION, key, use_param):
    def debug_model(self, ITERATION, key, use_grad):
        use_grad = bool(use_grad)
        tensors = [
            (p.main_grad.float() if use_grad else p.float())
            for m in self.models for p in m.parameters()
        ]
        # pax(0, {
        #     "params" : params,
        #     "params / abs" : [ torch.abs(p) for p in params ],
        #     "params / abs / sum" : [ torch.sum(torch.abs(p)) for p in params ],
        # })
        count = sum(t.nelement() for t in tensors)
        return self.debug_general(
            ITERATION,
            "model/%s, %s [count %d]" % (
                "grad" if use_grad else "param",
                key,
                count,
            ),
            # sum(torch.sum(torch.abs(t)) for t in tensors).item() / count,
            sum(torch.sum(torch.abs(t)) for t in tensors),
        )
    # def debug_model_param(self, ITERATION, key):
    #     return self._debug_model(ITERATION, key, True)
    # def debug_model_grad(self, ITERATION, key):
    #     return self._debug_model(ITERATION, key, False)

    # def _debug_main(self, ITERATION, key0, key1, f, ff):
    #     count = sum(
    #         p.nelement()
    #         for g in self.optimizer.param_groups
    #         for p in g["params"]
    #     )
    #     return self.debug_general(
    #         ITERATION,
    #         "main/%s, %s [count %d]" % (key1, key0, count),
    #         sum(ff(f(p))
    #             for g in self.optimizer.param_groups
    #             for p in g["params"]).item() / count,
    #     )
    # def debug_main_param(self, ITERATION, key):
    #     return self._debug_main(
    #         ITERATION,
    #         key,
    #         "param", # sum",
    #         # lambda p : p,
    #         lambda p : torch.abs(p),
    #         torch.sum,
    #     )
    # def debug_main_grad(self, ITERATION, key):
    #     return self._debug_main(
    #         ITERATION,
    #         key,
    #         "grad", # sum",
    #         # lambda p : p.grad,
    #         lambda p : torch.abs(p.grad),
    #         torch.sum,
    #     )
    # def _debug_main(self, ITERATION, key, use_param):
    def debug_main(self, ITERATION, key, use_grad):
        use_grad = bool(use_grad)
        tensors = [
            p.grad if use_grad else p
            for g in self.optimizer.param_groups
            for p in g["params"]
        ]
        tensors = [ t.float() for t in tensors ]
        count = sum(t.nelement() for t in tensors)
        return self.debug_general(
            ITERATION,
            "main/%s, %s [count %d]" % (
                "grad" if use_grad else "param",
                key,
                count,
            ),
            sum(torch.sum(torch.abs(t)) for t in tensors),
        )
    # def debug_main_param(self, ITERATION, key):
    #     return self._debug_main(ITERATION, key, True)
    # def debug_main_grad(self, ITERATION, key):
    #     return self._debug_main(ITERATION, key, False)
    # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

    @torch.no_grad()
    def step(self, ITERATION):

        timers = get_timers()

        # >>>
        # self.debug_model_param(ITERATION, "before copy grad.")
        # self.debug_model_grad(ITERATION, "before copy grad.")
        # self.debug_main_param(ITERATION, "before copy grad.")
        # self.debug_main_grad(ITERATION, "before copy grad.")
        # <<<

        # Copy gradients from model params to main params.
        timers('optimizer-copy-to-main-grad').start()
        self._copy_model_grads_to_main_grads(ITERATION)
        timers('optimizer-copy-to-main-grad').stop()

        # >>>
        # self.debug_model(ITERATION, "after copy grad.", 0)
        # self.debug_main(ITERATION, "after copy grad.", 1)
        # <<<

        # Do unscale, check for inf, and update grad scaler only for
        # the case that grad scaler is provided.
        if self.grad_scaler:

            # Unscale and check for inf/nan.
            timers('optimizer-unscale-and-check-inf').start()
            found_inf_flag = self._unscale_main_grads_and_check_for_nan()
            timers('optimizer-unscale-and-check-inf').stop()

            # We are done with scaling gradients
            # so we can update the loss scale.
            self.grad_scaler.update(found_inf_flag)

            # If we found inf/nan, skip the update.
            if found_inf_flag:
                pax(0, {
                    "main params" : self.get_main_params(),
                    "main grads" : self.get_main_grads(),
                    "found_inf_flag" : found_inf_flag,
                })
                return False, None, None

        # Clip the main gradients.
        timers('optimizer-clip-main-grad').start()
        grad_norm = None
        if self.clip_grad > 0.0:
            grad_norm = self.clip_grad_norm(self.clip_grad, ITERATION)
        timers('optimizer-clip-main-grad').stop()

        # count the zeros in the grads
        num_zeros_in_grad = self.count_zeros() if \
                            self.log_num_zeros_in_grad else None

        # >>>
        # param = self.optimizer.param_groups[0]["params"][0]
        # pax(0, {
        #     "param" : tp(param),
        #     "grad" : tp(param.grad),
        # })
        # <<<

        # >>>
        # self.debug_main(ITERATION, "before step.", 0)
        # <<<

        # Step the optimizer.
        self.optimizer.step()

        # >>>
        # self.debug_main(ITERATION, "after step.", 0)
        # <<<

        # Update params from main params.
        timers('optimizer-copy-main-to-model-params').start()
        self._copy_main_params_to_model_params(ITERATION)
        timers('optimizer-copy-main-to-model-params').stop()

        # >>>
        # self.debug_main_param(ITERATION, "after copy param.")
        # self.debug_main_grad(ITERATION, "after copy param.")
        # <<<

        # Successful update.
        return True, grad_norm, num_zeros_in_grad


# class Float16OptimizerWithFloat16Params(MegatronOptimizer):
class Float16OptimizerWithFloat16Params(BaseFloat16Optimizer):
    """Float16 optimizer for fp16 and bf16 data types.

    Arguments:
        optimizer: base optimizer such as Adam or SGD
        clip_grad: clip gradeints with this global L2 norm. Note
            that clipping is ignored if clip_grad == 0
        log_num_zeros_in_grad: return number of zeros in the gradients.
        params_have_main_grad: flag indicating if parameters have
            a `main_grad` field. If this is set, we are assuming
            that the model parameters are store in the `main_grad`
            field instead of the typical `grad` field. This happens
            for the DDP cases where there is a continuous buffer
            holding the gradients. For example for bfloat16, we want
            to do gradient accumulation and all-reduces in float32
            and as a result we store those gradients in the main_grad.
            Note that main grad is not necessarily in float32.
        bf16: if true, the model is running in bfloat16.
        grad_scaler: used for scaling gradients. Note that this can be
            None. This case happens when `bf16 = True` and we don't
            use any loss scale. Note that for `bf16 = True`, we can have
            a constnat gradient scaler. Also for `bf16 = False`, we
            always require a grad scaler.
    """

    def __init__(self, optimizer, clip_grad, log_num_zeros_in_grad,
                 params_have_main_grad, use_contiguous_buffers_in_local_ddp,
                 bf16, grad_scaler, models):

        super().__init__(
            optimizer, clip_grad, log_num_zeros_in_grad,
            params_have_main_grad, use_contiguous_buffers_in_local_ddp,
            bf16, grad_scaler, models)

        # ======================
        # main parameter stuff
        # ======================

        # Three groups of parameters:
        #   float16_groups: original float16 parameters
        #   fp32_from_float16_groups: fp32 copy of float16 parameters
        #   fp32_from_fp32_groups: original fp32 parameters
        self.float16_groups = []
        self.fp32_from_float16_groups = []
        self.fp32_from_fp32_groups = []

        # For all the groups in the original optimizer:
        for param_group in self.optimizer.param_groups:
            float16_params_this_group = []
            fp32_params_this_group = []
            fp32_from_float16_params_this_group = []
            # For all the parameters in this group:
            for i, param in enumerate(param_group['params']):
                if param.requires_grad:

                    # float16 params:
                    if param.type() in ['torch.cuda.HalfTensor',
                                        'torch.cuda.BFloat16Tensor']:
                        float16_params_this_group.append(param)
                        # Create a copy
                        main_param = param.detach().clone().float()
                        # Copy tensor model parallel attributes.
                        mpu.copy_tensor_model_parallel_attributes(main_param,
                                                                  param)
                        if hasattr(param, 'shared'):
                            main_param.shared = param.shared
                        # Replace the optimizer params with the new fp32 copy.
                        param_group['params'][i] = main_param

                        fp32_from_float16_params_this_group.append(main_param)
                        # Reset existing state dict key to the new main param.
                        if param in self.optimizer.state:
                            # >>>
                            raise Exception("hi.")
                            # <<<
                            self.optimizer.state[main_param] \
                                = self.optimizer.state.pop(param)

                    # fp32 params.
                    elif param.type() == 'torch.cuda.FloatTensor':
                        # >>>
                        pax(0, {"param": param})
                        # <<<
                        fp32_params_this_group.append(param)
                        param_group['params'][i] = param

                    else:
                        raise TypeError('Wrapped parameters must be one of '
                                        'torch.cuda.FloatTensor,  '
                                        'torch.cuda.HalfTensor, or '
                                        'torch.cuda.BFloat16Tensor. '
                                        'Received {}'.format(param.type()))

            self.float16_groups.append(float16_params_this_group)
            self.fp32_from_float16_groups.append(
                fp32_from_float16_params_this_group)
            self.fp32_from_fp32_groups.append(fp32_params_this_group)

        # Leverage state_dict() and load_state_dict() to
        # recast preexisting per-param state tensors
        self.optimizer.load_state_dict(self.optimizer.state_dict())

        # >>>
        # from megatron.mpu.layers import param_is_not_tensor_parallel_duplicate
        # params = self.get_parameters()
        # pax(0, {
        #     # "params / 0" : params[0],
        #     "params" : [ (p.tensor_model_parallel, tp(p)) for p in params ],
        #     "grads" : [ (param_is_not_tensor_parallel_duplicate(p.grad), tp(p.grad)) for p in params ],
        # })
        # <<<


    def zero_grad(self, set_to_none=True):
        """We only need to zero the model related parameters, i.e.,
        float16_groups & fp32_from_fp32_groups. We additionally zero
        fp32_from_float16_groups as a memory optimization to reduce
        fragmentation; in the case of set_to_none==True, the space
        used by this field can be safely deallocated at this point."""
        for group in self.float16_groups:
            _zero_grad_group_helper(group, set_to_none)
        for group in self.fp32_from_float16_groups:
            _zero_grad_group_helper(group, set_to_none)
        for group in self.fp32_from_fp32_groups:
            _zero_grad_group_helper(group, set_to_none)


    # >>>
    def reduce_grads(self, model):

        # >>>
        from torch.nn.parallel.distributed import DistributedDataParallel as torchDDP

        from megatron import get_args
        from megatron import get_timers
        from megatron.model import DistributedDataParallel as LocalDDP
        from megatron.model import Float16Module
        from megatron.utils import unwrap_model

        args = get_args()
        timers = get_timers()
        # <<<

        # All-reduce if needed.
        if args.DDP_impl == 'local':
            timers('backward-params-all-reduce').start()
            for model_module in model:
                model_module.allreduce_gradients()
            timers('backward-params-all-reduce').stop()

        # All-reduce word_embeddings' grad across first and last stages to ensure
        # that word_embeddings parameters stay in sync.
        # This should only run for models that support pipelined model parallelism
        # (BERT and GPT-2).
        timers('backward-embedding-all-reduce').start()
        if mpu.is_rank_in_embedding_group(ignore_virtual=True) and \
                mpu.get_pipeline_model_parallel_world_size() > 1:
            # >>>
            # raise Exception("[main] ready for weight sync?")
            # <<<
            if mpu.is_pipeline_first_stage(ignore_virtual=True):
                unwrapped_model = model[0]
            elif mpu.is_pipeline_last_stage(ignore_virtual=True):
                unwrapped_model = model[-1]
            else:  # We do not support the interleaved schedule for T5 yet.
                unwrapped_model = model[0]
            unwrapped_model = unwrap_model(
                unwrapped_model, (torchDDP, LocalDDP, Float16Module))

            if unwrapped_model.share_word_embeddings:
                word_embeddings_weight = unwrapped_model.word_embeddings_weight()
                # >>>
                if args.DDP_impl == 'local':
                    grad = word_embeddings_weight.main_grad
                else:
                    grad = word_embeddings_weight.grad
                torch.distributed.all_reduce(grad, group=mpu.get_embedding_group())
                # +++
                # grad_shard = optimizer.get_grad_shard(word_embeddings)
                # torch.distributed.all_reduce(grad_shard,
                #                              group=mpu.get_embedding_group())
                # <<<

        # All-reduce position_embeddings grad across first (encoder) and split (decoder) 
        # stages to ensure that position embeddings parameters stay in sync.
        # This should only run for T5 models with pipeline parallelism
        if mpu.is_rank_in_position_embedding_group() and \
                mpu.get_pipeline_model_parallel_world_size() > 1 and \
                args.pipeline_model_parallel_split_rank is not None:
            # >>>
            raise Exception("[main] ready for t5 sync?")
            # <<<
            unwrapped_model = model[0]
            unwrapped_model = unwrap_model(
                unwrapped_model, (torchDDP, LocalDDP, Float16Module))
            assert args.DDP_impl == 'local', \
                'T5 model is only supported with local DDP mode'
            # >>>
            grad = unwrapped_model.language_model.embedding.position_embeddings.weight.main_grad
            torch.distributed.all_reduce(grad, group=mpu.get_position_embedding_group())
            # +++
            # grad_shard = optimizer.get_grad_shard(
            #     unwrapped_model.language_model.embedding.position_embeddings.weight)
            # torch.distributed.all_reduce(grad_shard,
            #                              group=mpu.get_position_embedding_group())
            # <<<
        timers('backward-embedding-all-reduce').stop()

    def gather_params(self, ITERATION):
        pass

    def _copy_model_grads_to_main_grads(self, ITERATION):
        # This only needs to be done for the float16 group.
        for model_group, main_group in zip(self.float16_groups,
                                           self.fp32_from_float16_groups):
            for model_param, main_param in zip(model_group, main_group):
                if self.params_have_main_grad and hasattr(model_param, 'main_grad'):
                    main_param.grad = model_param.main_grad.float()
                else:
                    if model_param.grad is not None:
                        main_param.grad = model_param.grad.float()

                # Safe to deallocate model's grad/main_grad after copying.
                # (If using contiguous buffers, main_grad's memory should
                # persist and therefore should not be deallocated.)
                model_param.grad = None
                if self.params_have_main_grad and \
                   not self.use_contiguous_buffers_in_local_ddp:
                    model_param.main_grad = None

        # For fp32 grads, we need to reset the grads to main grad.
        if self.params_have_main_grad:
            for model_group in self.fp32_from_fp32_groups:
                for model_param in model_group:
                    model_param.grad = model_param.main_grad

                    # Safe to de-reference model's main_grad after copying.
                    # (If using contiguous buffers, main_grad's memory should
                    # persist and therefore should not be deallocated.)
                    if not self.use_contiguous_buffers_in_local_ddp:
                        model_param.main_grad = None

        # >>>
        # if ITERATION == DEBUG_ITERATION:
        #     pax(0, {
        #         "** branch **" : "** main. **",
        #         "ITERATION" : ITERATION,
        #         "model grads" :
        #         [ p.main_grad for m in self.models for p in m.parameters() ],
        #     })
        # <<<

    def _collect_main_grad_data_for_unscaling(self):

        main_grads = []

        # fp32 params from float16 ones.
        for main_group in self.fp32_from_float16_groups:
            for main_param in main_group:
                if main_param.grad is not None:
                    main_grads.append(main_param.grad.data)

        # pax(1, {"main_grads": main_grads})

        # Append fp32 parameters.
        for main_group in self.fp32_from_fp32_groups:
            for main_param in main_group:
                if main_param.grad is not None:
                    main_grads.append(main_param.grad.data)
        
        # >>>
        # from megatron.mpu.layers import param_is_not_tensor_parallel_duplicate
        # pax(1, {"main_grads": [ (param_is_not_tensor_parallel_duplicate(t), tp(t)) for t in main_grads ]})
        # <<<

        return main_grads


    def _get_model_and_main_params_data_float16(self):
        model_data = []
        main_data = []
        for model_group, main_group in zip(self.float16_groups,
                                           self.fp32_from_float16_groups):
            for model_param, main_param in zip(model_group, main_group):
                model_data.append(model_param.data)
                main_data.append(main_param.data)
        return model_data, main_data


    def _copy_main_params_to_model_params(self, ITERATION):
        # Only needed for the float16 params.
        model_data, main_data = self._get_model_and_main_params_data_float16()
        _multi_tensor_copy_this_to_that(this=main_data, that=model_data,
                                        overflow_buf=self._dummy_overflow_buf)
        # >>>
        # if ITERATION == DEBUG_ITERATION:
        #     pax(0, {
        #         "** branch **" : "** main. **",
        #         "ITERATION" : ITERATION,
        #         "model params" : [p for m in self.models for p in m.parameters()],
        #     })
        # <<<


    def _copy_model_params_to_main_params(self):
        # Only needed for the float16 params.
        model_data, main_data = self._get_model_and_main_params_data_float16()
        _multi_tensor_copy_this_to_that(this=model_data, that=main_data,
                                        overflow_buf=self._dummy_overflow_buf)


    def state_dict(self):
        state_dict = {}
        state_dict['optimizer'] = self.optimizer.state_dict()
        if self.grad_scaler:
            state_dict['grad_scaler'] = self.grad_scaler.state_dict()
        state_dict['fp32_from_fp16_params'] = self.fp32_from_float16_groups
        return state_dict


    def load_state_dict(self, state_dict):
        # Optimizer.
        optimizer_key = 'optimizer'
        if optimizer_key not in state_dict:
            optimizer_key = 'optimizer_state_dict'
            print_rank_0('***WARNING*** loading optimizer from '
                         'an old checkpoint ...')
        self.optimizer.load_state_dict(state_dict[optimizer_key])

        # Grad scaler.
        if 'grad_scaler' not in state_dict:
            print_rank_0('***WARNING*** found an old checkpoint, will not '
                         'load grad scaler ...')
        else:
            if self.grad_scaler:
                self.grad_scaler.load_state_dict(state_dict['grad_scaler'])
            else:
                print_rank_0('***WARNING*** fould the grad scaler in the '
                             'checkpoint but it is None in the class. '
                             'Skipping loading grad scaler ...')

        # Copy data for the main params.
        fp32_from_float16_params_key = 'fp32_from_fp16_params'
        if fp32_from_float16_params_key not in state_dict:
            fp32_from_float16_params_key = 'fp32_from_fp16'
        for current_group, saved_group in zip(
                self.fp32_from_float16_groups,
                state_dict[fp32_from_float16_params_key]):
            for current_param, saved_param in zip(current_group, saved_group):
                current_param.data.copy_(saved_param.data)


# >>>
import math

from megatron import get_args

# class ShardIndex:
class Shard:
    def __init__(self, start, end):
        self.start = start
        self.end = end
        self.size = end - start
    def normalize(self, start = 0):
        return Shard(start, start + self.size)
    def __str__(self):
        return "%d,%d [%d]" % (self.start, self.end, self.size)

# class Float16DistributedOptimizer(Float16OptimizerWithFloat16Params):
# class Float16DistributedOptimizer(MegatronOptimizer):
class Float16DistributedOptimizer(BaseFloat16Optimizer):

    # >>>
    # @classmethod
    # def test_reduce_scatter(cls):

    #     torch.manual_seed(mpu.get_data_parallel_rank())
    #     size = (20,)
    #     dtype = torch.float
    #     device = torch.cuda.current_device()
    #     data_parallel_world_size = mpu.get_data_parallel_world_size()
    #     data_parallel_group = mpu.get_data_parallel_group()

    #     input_list = [
    #         # torch.randn(size, dtype = dtype, device = device)
    #         5 * torch.randint(low = 1, high = 3, size = size, dtype = dtype, device = device)
    #         for _ in range(data_parallel_world_size)
    #     ]
    #     output = torch.empty(size, dtype = dtype, device = device)

    #     torch.distributed.reduce_scatter(
    #         output,
    #         input_list,
    #         group = data_parallel_group,
    #     )

    #     if torch.distributed.get_rank() == 0:
    #         print(output)
    #     pax(0, {
    #         "data_parallel_world_size" : data_parallel_world_size,
    #         "data_parallel_group" : data_parallel_group,
    #         "input_list" : input_list,
    #         "output" : tp(output),
    #     })
    # <<<

    @classmethod
    def get_model_gbuf_param_shard_map(cls, model, dtype, gbuf_world_shard):

        # Param shard map.
        param_world_index_map = model._grad_buffer_param_index_map[dtype]
        param_shard_map = {}
        for param, param_world_indexes in param_world_index_map.items():

            # Shard range.
            param_world_start, param_world_end = param_world_indexes
            param_local_start = max(
                0,
                param_world_start - gbuf_world_shard.start)
            param_local_end = min(
                gbuf_world_shard.size,
                param_world_end - gbuf_world_shard.start)

            # Add shard, if within range.
            if param_local_end > param_local_start:
                param_local_shard = Shard(param_local_start, param_local_end)
                # param_world_shard = param_local_shard.normalize(param_world_start)
                param_world_shard = param_local_shard.normalize(
                    param_local_start + gbuf_world_shard.start)
                sub_param_start = max(0, gbuf_world_shard.start-param_world_start)
                sub_param_shard = param_local_shard.normalize(sub_param_start)
                param_shard_map[param] = {
                    "gbuf_world" : param_world_shard,
                    "gbuf_local" : param_local_shard,
                    "param" : sub_param_shard,
                }

        # pax(0, {"param_shard_map": [ str((str(p.shape), s)) for p,s in param_shard_map.items() ]})

        return param_shard_map

    @classmethod
    def get_model_gbuf_shard(cls, model, dtype):

        data_parallel_rank = mpu.get_data_parallel_rank()
        data_parallel_world_size = mpu.get_data_parallel_world_size()

        # Grad buffer shard.
        grad_buffer = model._grad_buffers[dtype]
        gbuf_size = grad_buffer.numel
        max_gbuf_shard_size = int(math.ceil(gbuf_size / data_parallel_world_size))

        gbuf_world_all_shards = []
        for r in range(data_parallel_world_size):
            gbuf_world_start = r * max_gbuf_shard_size
            gbuf_world_end = min(gbuf_size, gbuf_world_start+max_gbuf_shard_size)
            gbuf_world_shard = Shard(gbuf_world_start, gbuf_world_end)
            gbuf_world_all_shards.append(gbuf_world_shard)
        gbuf_world_shard = gbuf_world_all_shards[data_parallel_rank]
        gbuf_local_shard = gbuf_world_shard.normalize()

        # Param shards.
        param_shard_map = cls.get_model_gbuf_param_shard_map(model,
                                                             dtype,
                                                             gbuf_world_shard)

        # Altogether.
        data = {
            "local" : gbuf_local_shard,
            "world" : gbuf_world_shard,
            "world_all" : gbuf_world_all_shards,
            "param_map" : param_shard_map,
        }

        # pax(1, {"data": data})

        return data

    @classmethod
    def get_model_gbuf_shard_map(cls, model):
        return {
            dtype : cls.get_model_gbuf_shard(model, dtype)
            for dtype in model._grad_buffers
        }

    @classmethod
    def get_param_gbuf_map(cls, model_gbuf_shards):

        param_gbuf_map = {}
        for model_index, model_gbuf_shard_map in enumerate(model_gbuf_shards):
            for dtype, gbuf_shard_map in model_gbuf_shard_map.items():
                for param, param_shard_map in gbuf_shard_map["param_map"].items():
                    # assert param not in param_size_map
                    # param_size_map[param] = param_shard_map["local"].size
                    param_gbuf_map[param] = (model_index, dtype)
                    # pax(0, {
                    #     "dtype" : dtype,
                    #     "gbuf_shard_map" : gbuf_shard_map,
                    #     "param" : tp(param),
                    #     "param_shard_map" : param_shard_map,
                    # })

        # pax(0, {
        #     "model_gbuf_shards" : model_gbuf_shards,
        #     # "param_size_map" :
        #     # [ (str(p.shape), s) for p, s in param_size_map.items() ],
        #     "param_gbuf_map" : param_gbuf_map,
        # })

        return param_gbuf_map

    @classmethod
    def get_optimizer_group_shards(cls, param_groups, model_gbuf_shards):

        num_groups = len(param_groups)

        # Param group map.
        param_group_map = {}
        for group_index, group in enumerate(param_groups):
            for param in group["params"]:
                assert param.requires_grad
                param_group_map[param] = group_index

        # Optimizer group shards.
        group_shards = [ {"size": 0, "param_map": {}} for _ in param_groups ]
        for model_gbuf_shard_map in model_gbuf_shards:
            for dtype, gbuf_shard_map in model_gbuf_shard_map.items():
                for param in gbuf_shard_map["param_map"]:
                    
                    group_index = param_group_map[param]
                    group_shard = group_shards[group_index]
                    param_size = gbuf_shard_map["param_map"][param]["param"].size

                    param_group_start = group_shard["size"]
                    param_group_end = param_group_start + param_size
                    param_group_shard = Shard(param_group_start, param_group_end)

                    group_shard["size"] += param_size
                    group_shard["param_map"][param] = param_group_shard

                    # >>>
                    # if torch.distributed.get_rank() == 1:
                    #     print(">>> [%d] ... group %d, size %d, param %s. <<<" % (
                    #         torch.distributed.get_rank(),
                    #         group_index,
                    #         param_size,
                    #         str(tuple(param.shape)),
                    #     ))
                    # <<<

        # Squeeze zero-size group shards.
        for group_index, group_shard in enumerate(group_shards):
            group_shard["orig_group"] = param_groups[group_index]
        group_shards = [ g for g in group_shards if g["size"] > 0 ]

        # pax(0, {
        #     "param_group_map": [
        #         (g, str(p.shape))
        #         for p, g in param_group_map.items()
        #     ],
        #     "group_shards" : group_shards,
        # })

        return group_shards

    @classmethod
    def allocate_main_param_shards(cls, opt_group_shards):

        # Allocate main param/grad shard.
        # ** torch.nn.Parameter ??
        # ** MemoryBuffer ??
        allocate_shard = lambda shard_size, dtype : torch.empty(
            (shard_size,),
            dtype = dtype,
            device = torch.cuda.current_device(),
            requires_grad = True)
        
        # main_param_shards = []
        for group_index, group_shard in enumerate(opt_group_shards):

            group_size = group_shard["size"]
            assert group_size != 0, "temporary check ... remove me."

            # ** todo: for dtype in model_main_dtypes ........ **

            # Allocate shard.
            # if group_size == 0:
            #     main_param = None
            # else:
            main_param = allocate_shard(group_size, torch.float)
            main_param.grad = allocate_shard(group_size, torch.float)
            mpu.set_tensor_model_parallel_attributes(main_param, True, 0, 1)

            # main_param_shards.append(main_param)
            group_shard["orig_group"]["params"] = [ main_param ]

            # # Update optimizer group.
            # self.optimizer.param_groups[group_index]["params"] = [ main_param ]

        # pax(1, {
        #     "opt_group_shards" : opt_group_shards,
        #     "main_param_shards" : main_param_shards,
        # })

        # return main_param_shards

    def __init__(self, optimizer, clip_grad, log_num_zeros_in_grad,
                 params_have_main_grad, use_contiguous_buffers_in_local_ddp,
                 bf16, grad_scaler, models):

        super().__init__(
            optimizer, clip_grad, log_num_zeros_in_grad,
            params_have_main_grad, use_contiguous_buffers_in_local_ddp,
            bf16, grad_scaler, models)

        # >>>
        args = get_args()
        assert args.use_contiguous_buffers_in_local_ddp # already checked in args
        # <<<

        # # Data parallel info.
        # self.data_parallel_group = mpu.get_data_parallel_group()
        # self.data_parallel_rank = mpu.get_data_parallel_rank()
        # self.data_parallel_world_size = mpu.get_data_parallel_world_size()

        # Model grad buffer shards.
        self.model_gbuf_shards = []
        for model_index, model in enumerate(self.models):
            self.model_gbuf_shards.append(self.get_model_gbuf_shard_map(model))
        self.param_gbuf_map = self.get_param_gbuf_map(self.model_gbuf_shards)

        # pax(0, {"param_gbuf_map": [ (str(tuple(p.shape)), d) for p, d in self.param_gbuf_map.items() ]})

        # Optimizer shards.
        self.opt_group_shards = self.get_optimizer_group_shards(
            self.optimizer.param_groups,
            self.model_gbuf_shards)

        # pax(0, {**{"opt_group_shards / %d" % i : g for i, g in enumerate(self.opt_group_shards)}})

        # Allocate main param shards.
        # self.main_param_shards = \
        #     self.allocate_main_param_shards(self.opt_group_shards)
        self.allocate_main_param_shards(self.opt_group_shards)

        # >>>
        # pax(0, {
        #     "model_gbuf_shards" : self.model_gbuf_shards,
        #     "opt_group_shards" : self.opt_group_shards,
        #     "main_param_shards" : self.main_param_shards,
        # })
        # <<<

        # Update optimizer groups.
        # - Also, leverage state_dict() and load_state_dict() to
        #   recast preexisting per-param state tensors.
        self.optimizer.param_groups = \
            [ g["orig_group"] for g in self.opt_group_shards ]
        self.optimizer.load_state_dict(self.optimizer.state_dict())

        # pax(0, {
        #     # "opt_group_shards" : self.opt_group_shards,
        #     # "param_groups" : self.optimizer.param_groups,
        #     "optimizer" : self.optimizer,
        #     "optimizer / state" : self.optimizer.state,
        # })
        # pax(1, {
        #     "optimizer" : self.optimizer,
        #     **{"optimizer / param_groups / %d" % i : g
        #        for i, g in enumerate(self.optimizer.param_groups)},
        #     "optimizer / state" : self.optimizer.state,
        #     "optimizer / state_dict" : self.optimizer.state_dict(),
        # })

        # Initialize main params.
        self._copy_model_params_to_main_params()

    @staticmethod
    def has_nan_debug(tensors):
        if isinstance(tensors, torch.Tensor):
            tensors = [ tensors ]
        assert isinstance(tensors, list)
        has_nans = [ (not torch.all(torch.isfinite(t)).item()) for t in tensors ]
        has_nan = any(has_nans)
        return has_nan
    def get_local_model_param_views(self):
        '''** FOR DEBUGGING. **'''
        model_param_views = []
        for group_index, opt_group_shard in enumerate(self.opt_group_shards):
            for param, opt_shard in opt_group_shard["param_map"].items():
                model_index, dtype = self.param_gbuf_map[param]
                gbuf_shard_map = \
                    self.model_gbuf_shards[model_index][dtype]["param_map"][param]
                model_param_shard = gbuf_shard_map["param"]
                model_param_views.append(
                    param.view(-1)[model_param_shard.start:model_param_shard.end])
        return model_param_views
    def get_local_model_grad_views(self):
        '''** FOR DEBUGGING. **'''
        model_grad_views = []
        for group_index, opt_group_shard in enumerate(self.opt_group_shards):
            for param, opt_shard in opt_group_shard["param_map"].items():
                model_index, dtype = self.param_gbuf_map[param]
                gbuf = self.models[model_index]._grad_buffers[dtype].data
                gbuf_shard_map = \
                    self.model_gbuf_shards[model_index][dtype]["param_map"][param]
                gbuf_world_shard = gbuf_shard_map["gbuf_world"]
                model_grad_views.append(
                    gbuf[gbuf_world_shard.start:gbuf_world_shard.end])
        return model_grad_views
    def get_world_model_params(self):
        '''** FOR DEBUGGING. **'''
        return [ p for m in self.models for p in m.parameters() ]
    def get_world_model_grads(self):
        '''** FOR DEBUGGING. **'''
        return [ p.main_grad for p in self.get_world_model_params() ]

    def get_main_params(self):
        return [ g["params"][0] for g in self.optimizer.param_groups ]
    def get_main_grads(self):
        return [ p.grad for p in self.get_main_params() ]
    def get_main_param(self, group_index):
        # return self.optimizer.param_groups[group_index]["params"][0]
        return self.get_main_params()[group_index]
    def get_main_grad(self, group_index):
        return self.get_main_param(group_index).grad

    def load_state_dict(self):
        raise Exception("hi.")
    def reload_model_params(self):
        raise Exception("hi.")
    def state_dict(self):
        raise Exception("hi.")

    def zero_grad(self, set_to_none=True):

        model_params = []
        for model in self.models:
            for dtype, param_map in model._grad_buffer_param_index_map.items():
                model_params.extend(param_map.keys())
        # main_params = []
        # for main_group in self.optimizer.param_groups:
        #     main_params.extend(main_group["params"])

        # ** using contiguous buffer; don't set_to_none **
        _zero_grad_group_helper(model_params, set_to_none = False) # set_to_none)
        # _zero_grad_group_helper(params, set_to_none = False)

        # pax(0, {"model_params": model_params})

    def get_model_grad_buffer_dp_views(self):

        # >>>
        # ** only contiguous grad buffer supported, for now [ TEMPORARY ] **
        args = get_args()
        assert args.use_contiguous_buffers_in_local_ddp
        # <<<

        # Grad buffer views.
        gbuf_view_items = []
        for model_index, model in enumerate(self.models):
            for dtype, gbuf_shard in self.model_gbuf_shards[model_index].items():
                world_shards = gbuf_shard["world_all"]
                gbuf = model._grad_buffers[dtype].data
                gbuf_views = [ gbuf[s.start:s.end] for s in world_shards ]
                gbuf_view_items.append((model_index, dtype, gbuf_views))

                # pax(0, {
                #     "world_shards" : world_shards,
                #     "gbuf_views" : gbuf_views,
                # })

        # pax(0, {"gbuf_view_items": gbuf_view_items})

        return gbuf_view_items

    def reduce_grads(self, model):

        # >>>
        from torch.nn.parallel.distributed import DistributedDataParallel as torchDDP

        from megatron import get_args
        from megatron import get_timers
        from megatron.model import DistributedDataParallel as LocalDDP
        from megatron.model import Float16Module
        from megatron.utils import unwrap_model

        args = get_args()
        timers = get_timers()
        # <<<

        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Sync word embedding params.

        # ... todo ...

        # All-reduce word_embeddings' grad across first and last stages to ensure
        # that word_embeddings parameters stay in sync.
        # This should only run for models that support pipelined model parallelism
        # (BERT and GPT-2).
        timers('backward-embedding-all-reduce').start()
        if mpu.is_rank_in_embedding_group(ignore_virtual=True) and \
                mpu.get_pipeline_model_parallel_world_size() > 1:
            # >>>
            # raise Exception("[fix] ready for weight sync?")
            # <<<
            if mpu.is_pipeline_first_stage(ignore_virtual=True):
                unwrapped_model = model[0]
            elif mpu.is_pipeline_last_stage(ignore_virtual=True):
                unwrapped_model = model[-1]
            else:  # We do not support the interleaved schedule for T5 yet.
                unwrapped_model = model[0]
            unwrapped_model = unwrap_model(
                unwrapped_model, (torchDDP, LocalDDP, Float16Module))

            if unwrapped_model.share_word_embeddings:
                word_embeddings_weight = unwrapped_model.word_embeddings_weight()
                # >>>
                if args.DDP_impl == 'local':
                    grad = word_embeddings_weight.main_grad
                else:
                    raise Exception("only 'main_grad' supported for distrib-opt.")
                    grad = word_embeddings_weight.grad
                torch.distributed.all_reduce(grad, group=mpu.get_embedding_group())
                # +++
                # grad_shard = optimizer.get_grad_shard(word_embeddings)
                # torch.distributed.all_reduce(grad_shard,
                #                              group=mpu.get_embedding_group())
                # <<<

        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Sync T5 position embedding params.

        # ... todo ...

        # All-reduce position_embeddings grad across first (encoder) and split (decoder) 
        # stages to ensure that position embeddings parameters stay in sync.
        # This should only run for T5 models with pipeline parallelism
        if mpu.is_rank_in_position_embedding_group() and \
                mpu.get_pipeline_model_parallel_world_size() > 1 and \
                args.pipeline_model_parallel_split_rank is not None:
            # >>>
            raise Exception("[fix] ready for t5 sync?")
            # <<<
            unwrapped_model = model[0]
            unwrapped_model = unwrap_model(
                unwrapped_model, (torchDDP, LocalDDP, Float16Module))
            assert args.DDP_impl == 'local', \
                'T5 model is only supported with local DDP mode'
            # >>>
            grad = unwrapped_model.language_model.embedding.position_embeddings.weight.main_grad
            torch.distributed.all_reduce(grad, group=mpu.get_position_embedding_group())
            # +++
            # grad_shard = optimizer.get_grad_shard(
            #     unwrapped_model.language_model.embedding.position_embeddings.weight)
            # torch.distributed.all_reduce(grad_shard,
            #                              group=mpu.get_position_embedding_group())
            # <<<
        timers('backward-embedding-all-reduce').stop()

        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Reduce-scatter.
        # timers('backward-params-reduce-scatter').start()
        timers('backward-params-all-reduce').start()
        data_parallel_rank = mpu.get_data_parallel_rank()
        data_parallel_world_size = mpu.get_data_parallel_world_size()
        data_parallel_group = mpu.get_data_parallel_group()

        gbuf_view_items = self.get_model_grad_buffer_dp_views()

        # pax(0, {"gbuf_views": [g for item in gbuf_view_items for g in item[2]]})
        # pax(0, {"gbufs": [
        #     g.data
        #     for m in self.models
        #     for g in m._grad_buffers.values()
        # ]})

        # >>>
        # buffer_.data /= mpu.get_data_parallel_world_size()
        # torch.distributed.all_reduce(
        #     buffer_.data, group=mpu.get_data_parallel_group())
        # <<<

        # >>>
        # self.debug_main_param(0, "before reduce scatter")
        # self.debug_main_grad(0, "before reduce scatter")
        # <<<

        for model_index, dtype, gbuf_views in gbuf_view_items:
            # coalesced /= mpu.get_data_parallel_world_size()
            gbuf = self.models[model_index]._grad_buffers[dtype].data

            # >>>
            # ~~ distributed.py ~~
            # gbuf /= data_parallel_world_size
            # torch.distributed.all_reduce(gbuf, group=data_parallel_group)
            # pax(0, {
            #     "gbuf" : tp(gbuf),
            # })
            # <<<

            # torch.mul(gbuf.data, 1. / data_parallel_world_size, out = gbuf.data)
            # gbuf_views = [ t / data_parallel_world_size for t in gbuf_views ]
            gbuf /= data_parallel_world_size

            # if 1:
            torch.distributed.reduce_scatter(
                gbuf_views[data_parallel_rank],
                gbuf_views,
                group = data_parallel_group,
            )
            # else:
            #     torch.distributed.all_reduce(
            #         gbuf,
            #         group = data_parallel_group,
            #     )
        # timers('backward-params-reduce-scatter').stop()
        timers('backward-params-all-reduce').stop()
            
        # pax(0, {"gbuf_views": [g for item in gbuf_view_items for g in item[2]]})

    def gather_params(self, ITERATION):

        # >>>
        timers = get_timers()
        # <<<

        timers('backward-params-all-gather').start()

        data_parallel_rank = mpu.get_data_parallel_rank()
        data_parallel_group = mpu.get_data_parallel_group()

        gbuf_view_items = self.get_model_grad_buffer_dp_views()

        # All-gather updated main params.
        for model_index, dtype, gbuf_views in gbuf_view_items:
            torch.distributed.all_gather(
                gbuf_views,
                gbuf_views[data_parallel_rank],
                group = data_parallel_group,
            )

        # Each model param now contains its updated values in its
        # '.main_grad' field.
        # for param in self.param_gbuf_map: # ... incomplete param list.
        for model in self.models:
            for dtype, param_map in model._grad_buffer_param_index_map.items():
                for param in param_map:
                    param.detach().copy_(param.main_grad)

        timers('backward-params-all-gather').stop()

        # pax(0, {"gbuf_view_items": gbuf_view_items})

        # >>>
        # self.debug_main(ITERATION, "after/inside gather_params.", 0)
        # self.debug_model(ITERATION, "after/inside gather_params.", 0)

        # if ITERATION == 2:
        #     pax(1, {
        #         "ITERATION" : ITERATION,
        #         # "gbufs" : [
        #         #     tp(b.data)
        #         #     for m in self.models
        #         #     for b in m._grad_buffers.values()
        #         # ],
        #         "param_gbuf_map" : [ str(tuple(p.shape)) for p in self.param_gbuf_map ],
        #     })
        # <<<

    def _collect_main_grad_data_for_unscaling(self):
        return [ g.data for g in self.get_main_grads() ]

    def _copy_model_params_to_main_params(self):

        for group_index, group_shard in enumerate(self.opt_group_shards):
            main_param = self.get_main_param(group_index)
            for model_param, main_shard in group_shard["param_map"].items():

                # Model shard.
                model_index, dtype = self.param_gbuf_map[model_param]
                model_shard = self.model_gbuf_shards \
                    [model_index][dtype]["param_map"][model_param]["param"]

                assert main_shard.size == model_shard.size

                # Copy shard data.
                main_view = main_param[main_shard.start:main_shard.end]
                model_view = model_param.view(-1)[model_shard.start:model_shard.end]

                main_view.detach().copy_(model_view)


    def _copy_model_grads_to_main_grads(self, ITERATION):

        for group_index, group_shard in enumerate(self.opt_group_shards):
            for model_param, main_shard in group_shard["param_map"].items():

                # Model shard.
                model_index, dtype = self.param_gbuf_map[model_param]
                model_shard = self.model_gbuf_shards \
                    [model_index][dtype]["param_map"][model_param]["gbuf_world"]

                assert main_shard.size == model_shard.size

                # pax(0, {
                #     "model_param" : tp(model_param),
                #     "main_shard" : str(main_shard),
                #     "param shard" : self.model_gbuf_shards \
                #     [model_index][dtype]["param_map"][model_param],
                # })

                # Copy from DDP's contiguous buffer to main shard's grad.
                model_grad = self.models[model_index]._grad_buffers[dtype].data
                main_grad = self.get_main_grad(group_index)

                # Copy sub-range within tensor.
                model_view = model_grad[model_shard.start:model_shard.end]
                main_view = main_grad[main_shard.start:main_shard.end]

                main_view.detach().copy_(model_view)

                # pax(0, {
                #     "group_index" : group_index,
                #     "group_shard" : group_shard,
                #     # "param" : tp(param),
                #     "model_index" : model_index,
                #     "dtype" : str(dtype),
                #     "model_grad" : tp(model_grad),
                #     "main_grad" : tp(main_grad),
                #     "model_view" : tp(model_view),
                #     "main_view" : tp(main_view),
                #     "model_shard" : str(model_shard),
                #     "main_shard" : str(main_shard),
                # })

        # >>>
        # if 1 or ITERATION == DEBUG_ITERATION:
        #     pax(0, {
        #         "** branch **" : "** fix. **",
        #         "ITERATION" : ITERATION,
        #         # "model grads" : self.get_world_model_grads(),
        #         "main_grads" : self.get_main_grads(),
        #         "group shards" : [
        #             "group %d; %s" % (grp_idx, main_shard)
        #             for grp_idx, grp_shard in enumerate(self.opt_group_shards)
        #             for model_param, main_shard in grp_shard["param_map"].items()
        #         ],
        #     })
        # <<<


    def _copy_main_params_to_model_params(self, ITERATION):

        for group_index, group_shard in enumerate(self.opt_group_shards):
            for model_param, main_shard in group_shard["param_map"].items():

                model_index, dtype = self.param_gbuf_map[model_param]
                model_shard = self.model_gbuf_shards \
                    [model_index][dtype]["param_map"][model_param]["gbuf_world"]

                assert main_shard.size == model_shard.size

                # Use DDP's contiguous buffer to temporarily hold params.
                model_param = self.models[model_index]._grad_buffers[dtype].data
                main_param = self.get_main_param(group_index)

                # Copy sub-range within tensor.
                model_view = model_param[model_shard.start:model_shard.end]
                main_view = main_param[main_shard.start:main_shard.end]

                model_view.detach().copy_(main_view)

                # Debug.
                # pax(1, {
                #     "group_index" : group_index,
                #     "group_shard" : group_shard,
                #     "model_param" : tp(model_param),
                #     "model_index" : model_index,
                #     "dtype" : str(dtype),
                #     "model_param" : tp(model_param),
                #     "main_param" : tp(main_param),
                #     "model_view" : tp(model_view),
                #     "main_view" : tp(main_view),
                #     "model_shard" : str(model_shard),
                #     "main_shard" : str(main_shard),
                # })

        # >>>
        # if ITERATION == DEBUG_ITERATION:
        #     pax(0, {
        #         "** branch **" : "** fix. **",
        #         "ITERATION" : ITERATION,
        #         "model params" : self.get_world_model_params(),
        #     })
        # <<<

# <<<


class FP32Optimizer(MegatronOptimizer):

    def __init__(self, optimizer, clip_grad,
                 log_num_zeros_in_grad,
                 params_have_main_grad,
                 use_contiguous_buffers_in_local_ddp):

        super(FP32Optimizer, self).__init__(
            optimizer, clip_grad, log_num_zeros_in_grad,
            params_have_main_grad, use_contiguous_buffers_in_local_ddp)

        self._scale = torch.cuda.FloatTensor([1.0])


    def zero_grad(self, set_to_none=True):
        """Copied from torch.optim.optimizer"""
        for group in self.optimizer.param_groups:
            _zero_grad_group_helper(group['params'], set_to_none)


    def get_loss_scale(self):
        """FP32 optimizer does not do any scaling."""
        return self._scale


    @torch.no_grad()
    def step(self):
        """Clip gradients (if needed) and step the base optimizer.
        Always return successful since there is no overflow."""

        # Copy main_grads to grads.
        if self.params_have_main_grad:
            for param_group in self.optimizer.param_groups:
                for param in param_group['params']:
                    param.grad = param.main_grad

                    # Safe to de-reference model's main_grad after copying.
                    # (If using contiguous buffers, main_grad's memory should
                    # persist and therefore should not be deallocated.)
                    if not self.use_contiguous_buffers_in_local_ddp:
                        param.main_grad = None

        # Clip gradients.
        grad_norm = None
        if self.clip_grad > 0.0:
            grad_norm = self.clip_grad_norm(self.clip_grad)

        # count the zeros in the grads
        num_zeros_in_grad = self.count_zeros() if \
                            self.log_num_zeros_in_grad else None

        # Update parameters.
        self.optimizer.step()

        # No overflow for FP32 optimizer.
        return True, grad_norm, num_zeros_in_grad


    def reload_model_params(self):
        pass


    def state_dict(self):
        return self.optimizer.state_dict()


    def load_state_dict(self, state_dict):
        self.optimizer.load_state_dict(state_dict)
