#!/bin/bash

export PYTHONUNBUFFERED=1

temp=(0)
job=0

for param in "${temp[@]}"; do
    for seed in 1 2 3 4; do
        name=hopper-$seed
        project_name=baseline-plan-to-predict
        save_dir=~/sfu/res/saved_models/RL/${project_name}
        mkdir -p ${save_dir}/${name}
        save_log_file=${save_dir}/${name}/out.log
            python main_mbpo.py --num_epoch 100 --env_name Hopper-v2 --seed $seed > $save_log_file 2>&1 &
            job=$((job+1))
            pids[$job]=$!
            names[$job]=$name
            sleep 5
    done
done

echo ${pids[*]}
echo ${names[*]}
for pid in ${pids[*]}; do
    wait $pid
done

date
