import os
from shutil import rmtree
from shutil import copyfile
from pathlib import Path
import inspect
from itertools import product
from concurrent.futures import ProcessPoolExecutor

import torch
from lagom.utils import pickle_dump
from lagom.utils import yaml_dump
from lagom.utils import ask_yes_or_no
from lagom.utils import timeit
from lagom.utils import color_str
from lagom.utils import CloudpickleWrapper


@timeit(color='green', attribute='bold')
def run_experiment(run, config, seeds, log_dir, max_workers, chunksize=1, use_gpu=False, gpu_ids=None):
    r"""A convenient function to parallelize the experiment (master-worker pipeline). 
    
    It is implemented by using `concurrent.futures.ProcessPoolExecutor`
    
    It automatically creates all subfolders for each pair of configuration and random seed
    to store the loggings of the experiment. The root folder is given by the user.
    Then all subfolders for each configuration are created with the name of their job IDs.
    Under each configuration subfolder, a set subfolders are created for each
    random seed (the random seed as folder name). Intuitively, an experiment could have 
    following directory structure::

        - logs
            - 0  # ID number
                - 123  # random seed
                - 345
                - 567
            - 1
                - 123
                - 345
                - 567
            - 2
                - 123
                - 345
                - 567
            - 3
                - 123
                - 345
                - 567
            - 4
                - 123
                - 345
                - 567
                
    Args:
        run (function): a function that defines an algorithm, it must take the 
            arguments `(config, seed, device, logdir)`
        config (Config): a :class:`Config` object defining all configuration settings
        seeds (list): a list of random seeds
        log_dir (str): a string to indicate the path to store loggings.
        max_workers (int): argument for ProcessPoolExecutor. if `None`, then all experiments run serially.
        chunksize (int): argument for Executor.map()
        use_gpu (bool): if `True`, then use CUDA. Otherwise, use CPU.
        gpu_ids (list): if `None`, then use all available GPUs. Otherwise, only use the
            GPU device defined in the list. 
    
    """
    configs = config.make_configs()
    
    # create logging dir
    log_path = Path(log_dir)
    if not log_path.exists():
        log_path.mkdir(parents=True)
    else:
        msg = f"Logging directory '{log_path.absolute()}' already existed, do you want to clean it ?"
        answer = ask_yes_or_no(msg)
        if answer:
            rmtree(log_path)
            log_path.mkdir(parents=True)
        else:  # back up
            old_log_path = log_path.with_name('old_' + log_path.name)
            log_path.rename(old_log_path)
            log_path.mkdir(parents=True)
            print(f"The old logging directory is renamed to '{old_log_path.absolute()}'. ")
            input('Please, press Enter to continue\n>>> ')

    # save source files
    source_path = Path(log_path / 'source_files/')
    source_path.mkdir(parents=True)
    [copyfile(s, source_path / s.name) for s in Path(inspect.getsourcefile(run)).parent.glob('*.py')]
    
    # Create subfolders for each ID and subsubfolders for each random seed
    for config in configs:
        ID = config['ID']
        for seed in seeds:
            p = log_path / f'{ID}' / f'{seed}'
            p.mkdir(parents=True)
        yaml_dump(obj=config, f=log_path / f'{ID}' / 'config', ext='.yml')
        
    pickle_dump(configs, log_path / 'configs', ext='.pkl')
    
    # Create unique id for each job
    jobs = list(enumerate(product(configs, seeds)))
    
    def _run(job):
        job_id, (config, seed) = job
        # VERY IMPORTANT TO AVOID GETTING STUCK, oversubscription
        # see following links
        # https://github.com/pytorch/pytorch/issues/19163
        # https://software.intel.com/en-us/intel-threading-building-blocks-openmp-or-native-threads
        torch.set_num_threads(1)
        if use_gpu:
            num_gpu = torch.cuda.device_count()
            if gpu_ids is None:  # use all GPUs
                device_id = job_id % num_gpu
            else:
                assert all([i >= 0 and i < num_gpu for i in gpu_ids])
                device_id = gpu_ids[job_id % len(gpu_ids)]
            torch.cuda.set_device(device_id)
            device = torch.device(f'cuda:{device_id}')
        else:
            device = torch.device('cpu')
            
        print(f'@ Experiment: ID: {config["ID"]} ({len(configs)}), Seed: {seed}, Device: {device}, Job: {job_id} ({len(jobs)}), PID: {os.getpid()}')
        print('#'*50)
        [print(f'# {key}: {value}') for key, value in config.items()]
        print('#'*50)
        
        logdir = log_path / f'{config["ID"]}' / f'{seed}'
        result = run(config, seed, device, logdir)
        # Release all un-freed GPU memory
        if use_gpu:
            torch.cuda.empty_cache()
        return result
    
    if max_workers is None:
        results = [_run(job) for job in jobs]
    else:
        with ProcessPoolExecutor(max_workers=min(max_workers, len(jobs))) as executor:
            results = list(executor.map(CloudpickleWrapper(_run), jobs, chunksize=chunksize))
    print(color_str(f'\nExperiment finished. Loggings are stored in {log_path.absolute()}. ', 'cyan', 'bold'))
    return results
