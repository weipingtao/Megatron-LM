# coding=utf-8
# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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

from megatron import get_retro_args
from tools.retro.db.utils import get_merged_train_dataset
from tools.retro.index.factory import IndexFactory
from tools.retro.utils import GPTToTextDataset

from .utils import get_index_dir

# >>>
from lutil import pax
# <<<


def add_to_index(timer):

    args = get_retro_args()

    workdir = get_index_dir()
    index = IndexFactory.get_index(args.retro_index_ty)

    gpt_dataset = get_merged_train_dataset()
    text_dataset = GPTToTextDataset(gpt_dataset)

    output_index_path = index.add(text_dataset, workdir, timer)
    return output_index_path


def remove_add_files(timer):

    # Single process only.
    if torch.distributed.get_rank() != 0:
        return

    # Get file paths.
    add_paths = [
        os.path.join(args.index_dir_path, r, n)
        for r, ds, fs in os.walk(args.index_dir_path)
        for n in [ *ds, *fs ]
        if n.startswith("add")
    ]

    # Remove files.
    for p in add_paths:
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.isfile(p):
            os.remove(p)
        else:
            raise Exception("specialize for this monster, '%s'." % p)
