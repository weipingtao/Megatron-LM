#!/bin/bash

if [ "$CORPUS" = "wiki-tiny" ]; then
    export RETRO_INDEX_STR="IVF4096_HNSW4,Flat"
    export RETRO_GPT_TRAIN_SAMPLES=31250
    export LR_DECAY_SAMPLES=2
    export LR_WARMUP_SAMPLES=1
    export RETRO_GPT_EVAL_INTERVAL=2000
    export RETRO_GPT_EVAL_ITERS=100
    export RETRO_EF_SEARCH=4
    export RETRO_NPROBE=64
    export BERT_EMBEDDER_TYPE=megatron
    export DATALOADER_TYPE=cyclic
fi
if [ "$CORPUS" = "wiki" ]; then
    export RETRO_INDEX_STR="IVF262144_HNSW32,Flat"
    export RETRO_GPT_TRAIN_SAMPLES=2037248
    export LR_DECAY_SAMPLES=2
    export LR_WARMUP_SAMPLES=1
    export RETRO_GPT_EVAL_INTERVAL=2000
    export RETRO_GPT_EVAL_ITERS=100
    export RETRO_EF_SEARCH=16
    export RETRO_NPROBE=4096
    export BERT_EMBEDDER_TYPE=megatron
    export DATALOADER_TYPE=cyclic
fi
if [ "$CORPUS" = "corpus" ]; then
    export RETRO_INDEX_STR="OPQ32_256,IVF4194304_HNSW32,PQ32"
    export RETRO_GPT_TRAIN_SAMPLES=192000000
    export LR_DECAY_SAMPLES=166400000
    export LR_WARMUP_SAMPLES=162761
    export RETRO_GPT_EVAL_INTERVAL=2000
    export RETRO_GPT_EVAL_ITERS=50
    export RETRO_EF_SEARCH=32
    export RETRO_NPROBE=4096
    export BERT_EMBEDDER_TYPE=huggingface
    export DATALOADER_TYPE=single
fi
