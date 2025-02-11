#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserve.
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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import time
import os
import random
import math
import contextlib

import paddle
import paddle.fluid as fluid
import paddle.fluid.framework as framework
import paddle.fluid.profiler as profiler
from paddle.fluid.executor import Executor

import reader

import sys
if sys.version[0] == '2':
    reload(sys)
    sys.setdefaultencoding("utf-8")
sys.path.append('..')
import os
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

from args import *
sys.path.append("../")
from models.language_model import lm_model
from config import RNNConfig
import logging
import pickle

SEED = 123


@contextlib.contextmanager
def profile_context(profile=True):
    if profile:
        with profiler.profiler('All', 'total', '/tmp/paddingrnn.profile'):
            yield
    else:
        yield


def get_current_model_para(train_prog, train_exe):
    param_list = train_prog.block(0).all_parameters()
    param_name_list = [p.name for p in param_list]

    vals = {}
    for p_name in param_name_list:
        p_array = np.array(fluid.global_scope().find_var(p_name).get_tensor())
        vals[p_name] = p_array

    return vals


def save_para_npz(train_prog, train_exe):
    print("begin to save model to model_base")
    param_list = train_prog.block(0).all_parameters()
    param_name_list = [p.name for p in param_list]

    vals = {}
    for p_name in param_name_list:
        p_array = np.array(fluid.global_scope().find_var(p_name).get_tensor())
        vals[p_name] = p_array

    emb = vals["embedding_para"]
    print("begin to save model to model_base")
    np.savez("mode_base", **vals)


def main():
    args = parse_args()
    logger = logging.getLogger("lm")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    if args.log_path:
        file_handler = logging.FileHandler(args.log_path)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    else:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    logger.info('Running with args : {}'.format(args))

    config = RNNConfig(args)

    # define train program
    main_program = fluid.Program()
    startup_program = fluid.Program()
    if args.enable_ce:
        startup_program.random_seed = SEED
    with fluid.program_guard(main_program, startup_program):
        with fluid.unique_name.guard():
            res_vars = lm_model.lm_model(
                config.hidden_size,
                config.vocab_size,
                config.batch_size,
                num_layers=config.num_layers,
                num_steps=config.num_steps,
                init_scale=config.init_scale,
                dropout=config.dropout,
                rnn_model=config.rnn_model,
                use_py_reader=args.use_py_reader)

            if args.use_py_reader:
                py_reader = res_vars[-1]
                res_vars = res_vars[:-1]
            loss, last_hidden, last_cell, feed_order = res_vars

            fluid.clip.set_gradient_clip(
                clip=fluid.clip.GradientClipByGlobalNorm(
                    clip_norm=config.max_grad_norm))

            learning_rate = fluid.layers.create_global_var(
                name="learning_rate",
                shape=[1],
                value=1.0,
                dtype='float32',
                persistable=True)

            optimizer = fluid.optimizer.SGD(learning_rate=learning_rate)
            optimizer.minimize(loss)

    # define inference program
    inference_program = fluid.Program()
    inference_startup_program = fluid.Program()
    with fluid.program_guard(inference_program, inference_startup_program):
        with fluid.unique_name.guard():
            lm_model.lm_model(
                config.hidden_size,
                config.vocab_size,
                config.batch_size,
                num_layers=config.num_layers,
                num_steps=config.num_steps,
                init_scale=config.init_scale,
                dropout=config.dropout,
                rnn_model=config.rnn_model,
                use_py_reader=False)
    # Some op behaves differently for train and inference, we need to call
    # this clone function to ensure every op is right for inference.
    inference_program = inference_program.clone(for_test=True)

    place = fluid.CUDAPlace(0) if args.use_gpu else fluid.CPUPlace()
    exe = Executor(place)
    exe.run(startup_program)

    device_count = len(fluid.cuda_places()) if args.use_gpu else len(
        fluid.cpu_places())

    exec_strategy = fluid.ExecutionStrategy()
    exec_strategy.num_threads = device_count
    exec_strategy.num_iteration_per_drop_scope = 100

    build_strategy = fluid.BuildStrategy()
    build_strategy.enable_inplace = True
    build_strategy.memory_optimize = False
    build_strategy.fuse_all_optimizer_ops = True

    if args.parallel:
        train_program = fluid.compiler.CompiledProgram(
            main_program).with_data_parallel(
                loss_name=loss.name,
                build_strategy=build_strategy,
                exec_strategy=exec_strategy)
    else:
        train_program = fluid.compiler.CompiledProgram(main_program)

    data_path = args.data_path
    print("begin to load data")
    raw_data = reader.ptb_raw_data(data_path)
    print("finished load data")
    train_data, valid_data, test_data, _ = raw_data

    def generate_init_data():
        init_hidden = np.zeros(
            (config.num_layers, config.batch_size, config.hidden_size),
            dtype='float32')
        init_cell = np.zeros(
            (config.num_layers, config.batch_size, config.hidden_size),
            dtype='float32')
        return init_hidden, init_cell

    def generate_new_lr(epoch_id=0, device_count=1):
        new_lr = config.base_learning_rate * (config.lr_decay**max(
            epoch_id + 1 - config.epoch_start_decay, 0.0))
        lr = np.ones((device_count), dtype='float32') * new_lr
        return lr

    def prepare_input(batch,
                      init_hidden=None,
                      init_cell=None,
                      epoch_id=0,
                      with_lr=True,
                      device_count=1):
        x, y = batch
        x = x.reshape((-1, config.num_steps, 1))
        y = y.reshape((-1, 1))

        res = {}
        res['x'] = x
        res['y'] = y
        if init_hidden is not None:
            res['init_hidden'] = init_hidden
        if init_cell is not None:
            res['init_cell'] = init_cell
        if with_lr:
            res['learning_rate'] = generate_new_lr(epoch_id, device_count)

        return res

    def eval(data):
        # when eval the batch_size set to 1
        eval_data_iter = reader.get_data_iter(data, config.batch_size,
                                              config.num_steps)
        total_loss = 0.0
        iters = 0
        init_hidden, init_cell = generate_init_data()
        for batch_id, batch in enumerate(eval_data_iter):
            input_data_feed = prepare_input(
                batch, init_hidden, init_cell, epoch_id=0, with_lr=False)
            fetch_outs = exe.run(
                program=inference_program,
                feed=input_data_feed,
                fetch_list=[loss.name, last_hidden.name, last_cell.name],
                use_program_cache=False)

            cost_eval = np.array(fetch_outs[0])
            init_hidden = np.array(fetch_outs[1])
            init_cell = np.array(fetch_outs[2])

            total_loss += cost_eval
            iters += config.num_steps

        ppl = np.exp(total_loss / iters)
        return ppl

    def get_log_interval(data_len):
        num_batchs = data_len // config.batch_size
        epoch_size = (num_batchs - 1) // config.num_steps
        log_interval = max(1, epoch_size // 10)
        return log_interval

    def train_an_epoch(epoch_id, batch_times):
        # get train epoch size
        log_interval = get_log_interval(len(train_data))
        train_data_iter = reader.get_data_iter(train_data, config.batch_size,
                                               config.num_steps)

        total_loss = 0
        iters = 0

        init_hidden, init_cell = generate_init_data()
        for batch_id, batch in enumerate(train_data_iter):
            input_data_feed = prepare_input(
                batch,
                init_hidden=init_hidden,
                init_cell=init_cell,
                epoch_id=epoch_id,
                with_lr=True,
                device_count=device_count)

            batch_start_time = time.time()
            fetch_outs = exe.run(train_program,
                                 feed=input_data_feed,
                                 fetch_list=[
                                     loss.name, "learning_rate",
                                     last_hidden.name, last_cell.name
                                 ],
                                 use_program_cache=True)
            batch_time = time.time() - batch_start_time
            batch_times.append(batch_time)

            cost_train = np.array(fetch_outs[0])
            lr = np.array(fetch_outs[1])
            init_hidden = np.array(fetch_outs[2])
            init_cell = np.array(fetch_outs[3])

            total_loss += cost_train
            iters += config.num_steps
            if batch_id > 0 and batch_id % log_interval == 0:
                ppl = np.exp(total_loss / iters)
                print(
                    "-- Epoch:[%d]; Batch:[%d]; Time: %.5f s; ppl: %.5f, lr: %.5f"
                    % (epoch_id, batch_id, batch_time, ppl[0], lr[0]))

        ppl = np.exp(total_loss / iters)
        return ppl

    def train_an_epoch_py_reader(epoch_id, batch_times):
        # get train epoch size
        log_interval = get_log_interval(len(train_data))

        init_hidden, init_cell = generate_init_data()

        total_loss = 0
        iters = 0

        py_reader.start()
        batch_id = 0
        try:
            while True:
                data_feeds = {}
                if batch_id == 0:
                    batch_time = 0
                    batch_start_time = time.time()
                else:
                    batch_time = time.time() - batch_start_time
                    batch_times.append(batch_time)
                    batch_start_time = time.time()

                new_lr = generate_new_lr(epoch_id, device_count)
                data_feeds['learning_rate'] = new_lr
                data_feeds["init_hidden"] = init_hidden
                data_feeds["init_cell"] = init_cell

                fetch_outs = exe.run(train_program,
                                     feed=data_feeds,
                                     fetch_list=[
                                         loss.name, "learning_rate",
                                         last_hidden.name, last_cell.name
                                     ],
                                     use_program_cache=True)

                cost_train = np.array(fetch_outs[0])
                lr = np.array(fetch_outs[1])
                init_hidden = np.array(fetch_outs[2])
                init_cell = np.array(fetch_outs[3])

                total_loss += cost_train
                iters += config.num_steps
                if batch_id > 0 and (log_interval == 0 or
                                     batch_id % log_interval == 0):
                    ppl = np.exp(total_loss / iters)
                    print(
                        "-- Epoch:[%d]; Batch:[%d]; Time: %.5f s; ppl: %.5f, lr: %.5f"
                        % (epoch_id, batch_id, batch_time, ppl[0], lr[0]))

                batch_id += 1
        except fluid.core.EOFException:
            py_reader.reset()

        batch_times.append(time.time() - batch_start_time)
        ppl = np.exp(total_loss / iters)
        return ppl

    def train():
        if args.use_py_reader:

            def data_gen():
                data_iter_size = config.batch_size // device_count
                train_batches = reader.get_data_iter(train_data, data_iter_size,
                                                     config.num_steps)
                for batch in train_batches:
                    x, y = batch
                    x = x.reshape((-1, config.num_steps, 1))
                    y = y.reshape((-1, 1))
                    yield x, y

            py_reader.decorate_tensor_provider(data_gen)

        total_time = 0.0
        for epoch_id in range(config.max_epoch):
            batch_times = []
            epoch_start_time = time.time()
            if args.use_py_reader:
                train_ppl = train_an_epoch_py_reader(epoch_id, batch_times)
            else:
                train_ppl = train_an_epoch(epoch_id, batch_times)
            epoch_time = time.time() - epoch_start_time
            total_time += epoch_time
            print(
                "\nTrain epoch:[%d]; epoch Time: %.5f; ppl: %.5f; avg_time: %.5f steps/s \n"
                % (epoch_id, epoch_time, train_ppl[0],
                   len(batch_times) / sum(batch_times)))

            # FIXME(zjl): ppl[0] increases as batch_size increases. 
            # We should find a better way to calculate ppl by normalizing batch_size. 
            if device_count == 1 and config.batch_size <= 20 and epoch_id == 0 and train_ppl[
                    0] > 1000:
                # for bad init, after first epoch, the loss is over 1000
                # no more need to continue
                print(
                    "Parameters are randomly initialized and not good this time because the loss is over 1000 after the first epoch."
                )
                print("Abort this training process and please start again.")
                return

            if epoch_id == config.max_epoch - 1 and args.enable_ce:
                # kpis
                print("ptblm\tlstm_language_model_%s_duration_card%d\t%s" %
                      (args.rnn_model, device_count,
                       total_time / config.max_epoch))
                print("ptblm\tlstm_language_model_%s_loss_card%d\t%s" %
                      (args.rnn_model, device_count, train_ppl[0]))

            # NOTE(zjl): sometimes we have not enough data for eval if batch_size is large, i.e., 2100
            # Just skip to avoid error
            def is_valid_data(data, batch_size, num_steps):
                data_len = len(data)
                batch_len = data_len // batch_size
                epoch_size = (batch_len - 1) // num_steps
                return epoch_size >= 1

            valid_data_valid = is_valid_data(valid_data, config.batch_size,
                                             config.num_steps)
            if valid_data_valid:
                valid_ppl = eval(valid_data)
                print("Valid ppl: %.5f" % valid_ppl[0])
            else:
                print(
                    'WARNING: length of valid_data is {}, which is not enough for batch_size {} and num_steps {}'.
                    format(
                        len(valid_data), config.batch_size, config.num_steps))

            save_model_dir = os.path.join(args.save_model_dir, str(epoch_id))
            fluid.io.save_persistables(
                executor=exe, dirname=save_model_dir, main_program=main_program)
            print("Saved model to: %s.\n" % save_model_dir)

    with profile_context(args.profile):
        train()

    test_ppl = eval(test_data)
    print("Test ppl:", test_ppl[0])


if __name__ == '__main__':
    main()
