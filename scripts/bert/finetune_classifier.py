"""
Sentence Pair Classification with Bidirectional Encoder Representations from Transformers

=========================================================================================

This example shows how to implement finetune a model with pre-trained BERT parameters for
sentence pair classification, with Gluon NLP Toolkit.

@article{devlin2018bert,
  title={BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding},
  author={Devlin, Jacob and Chang, Ming-Wei and Lee, Kenton and Toutanova, Kristina},
  journal={arXiv preprint arXiv:1810.04805},
  year={2018}
}
"""

# coding: utf-8

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint:disable=redefined-outer-name,logging-format-interpolation

import os
import time
import argparse
import random
import logging
import warnings
import numpy as np
import mxnet as mx
from mxnet import gluon
import gluonnlp as nlp
from gluonnlp.model import bert_12_768_12
from bert import BERTClassifier, BERTRegression
from tokenizer import FullTokenizer
from dataset import MRPCDataset, QQPDataset, RTEDataset, \
    STSBDataset, ClassificationTransform, RegressionTransform, \
    QNLIDataset, COLADataset, MNLIDataset, WNLIDataset, SSTDataset

tasks = {
    'MRPC': MRPCDataset,
    'QQP': QQPDataset,
    'QNLI': QNLIDataset,
    'RTE': RTEDataset,
    'STS-B': STSBDataset,
    'CoLA': COLADataset,
    'MNLI': MNLIDataset,
    'WNLI': WNLIDataset,
    'SST': SSTDataset
}

parser = argparse.ArgumentParser(
    description='BERT fine-tune examples for GLUE tasks.')
parser.add_argument('--epochs', type=int, default=3, help='number of epochs')
parser.add_argument(
    '--batch_size',
    type=int,
    default=32,
    help='Batch size. Number of examples per gpu in a minibatch')
parser.add_argument(
    '--test_batch_size', type=int, default=8, help='Test batch size')
parser.add_argument(
    '--optimizer', type=str, default='adam', help='optimization algorithm')
parser.add_argument(
    '--task_name',
    type=str,
    choices=tasks.keys(),
    help='The name of the task to fine-tune.(MRPC,...)')
parser.add_argument(
    '--lr', type=float, default=5e-5, help='Initial learning rate')
parser.add_argument(
    '--warmup_ratio',
    type=float,
    default=0.1,
    help='ratio of warmup steps used in NOAM\'s stepsize schedule')
parser.add_argument(
    '--log_interval', type=int, default=10, help='report interval')
parser.add_argument(
    '--max_len',
    type=int,
    default=128,
    help='Maximum length of the sentence pairs')
parser.add_argument(
    '--gpu', action='store_true', help='whether to use gpu for finetuning')

parser.add_argument(
    '--seed', type=int, default=2, help='Random seed, default is 2')
parser.add_argument(
    '--accumulate',
    type=int,
    default=None,
    help='The number of batches for '
    'gradients accumulation to simulate large batch size. Default is None')
parser.add_argument(
    '--dev_batch_size',
    type=int,
    default=8,
    help='Batch size for dev set, default is 8')
args = parser.parse_args()

logging.getLogger().setLevel(logging.DEBUG)
logging.info(args)

batch_size = args.batch_size
dev_batch_size = args.dev_batch_size
task_name = args.task_name
lr = args.lr
accumulate = args.accumulate
log_interval = args.log_interval * accumulate if accumulate else args.log_interval
if accumulate:
    logging.info("Using gradient accumulation. Effective batch size = %d" %
                 (accumulate * batch_size))

# random seed
np.random.seed(args.seed)
random.seed(args.seed)
mx.random.seed(args.seed)

ctx = mx.cpu() if not args.gpu else mx.gpu()

# model and loss
dataset = 'book_corpus_wiki_en_uncased'
bert, vocabulary = bert_12_768_12(
    dataset_name=dataset,
    pretrained=True,
    ctx=ctx,
    use_pooler=True,
    use_decoder=False,
    use_classifier=False)
do_lower_case = 'uncased' in dataset
tokenizer = FullTokenizer(vocabulary, do_lower_case=do_lower_case)

if task_name in ['STS-B']:
    model = BERTRegression(bert, dropout=0.1)
    model.regression.initialize(init=mx.init.Normal(0.02), ctx=ctx)
    loss_function = gluon.loss.L2Loss()
else:
    model = BERTClassifier(bert, dropout=0.1)
    model.classifier.initialize(init=mx.init.Normal(0.02), ctx=ctx)
    loss_function = gluon.loss.SoftmaxCELoss()

logging.info(model)
model.hybridize(static_alloc=True)
loss_function.hybridize(static_alloc=True)


def preprocess_data(tokenizer, task_name, batch_size, dev_batch_size, max_len):
    """Data preparation function."""
    # transformation
    task = tasks[task_name]

    if task_name in ['STS-B']:
        train_trans = RegressionTransform(tokenizer, max_len, pad=False)
        dev_trans = RegressionTransform(tokenizer, max_len)
    elif task_name in ['CoLA', 'SST']:
        train_trans = ClassificationTransform(
            tokenizer, task.get_labels(), max_len, pad=False, pair=False)
        dev_trans = ClassificationTransform(
            tokenizer, task.get_labels(), max_len, pair=False)
    else:
        train_trans = ClassificationTransform(
            tokenizer, task.get_labels(), max_len, pad=False, pair=True)
        dev_trans = ClassificationTransform(
            tokenizer, task.get_labels(), max_len, pair=True)

    if task_name == 'MNLI':
        data_train = task('dev_matched').transform(train_trans, lazy=False)
        data_dev = task('dev_mismatched').transform(dev_trans, lazy=False)
    else:
        data_train = task('train').transform(train_trans)
        data_dev = task('dev').transform(dev_trans)

    data_train_len = data_train.transform(
        lambda input_id, length, segment_id, label_id: length)
    num_samples_train = len(data_train)
    # bucket sampler
    batchify_fn = nlp.data.batchify.Tuple(
        nlp.data.batchify.Pad(axis=0), nlp.data.batchify.Stack(),
        nlp.data.batchify.Pad(axis=0), nlp.data.batchify.Stack())
    batch_sampler = nlp.data.sampler.FixedBucketSampler(
        data_train_len,
        batch_size=batch_size,
        num_buckets=10,
        ratio=0,
        shuffle=True)
    # data loaders
    dataloader = gluon.data.DataLoader(
        dataset=data_train,
        num_workers=1,
        batch_sampler=batch_sampler,
        batchify_fn=batchify_fn)
    dataloader_dev = mx.gluon.data.DataLoader(
        data_dev, batch_size=dev_batch_size, num_workers=1, shuffle=False)
    return dataloader, dataloader_dev, num_samples_train


train_data, dev_data, num_train_examples = preprocess_data(
    tokenizer, task_name, batch_size, dev_batch_size, args.max_len)


def evaluate(metric):
    """Evaluate the model on validation dataset.
    """
    step_loss = 0
    metric.reset()
    for _, seqs in enumerate(dev_data):
        Ls = []
        input_ids, valid_len, type_ids, label = seqs
        out = model(
            input_ids.as_in_context(ctx), type_ids.as_in_context(ctx),
            valid_len.astype('float32').as_in_context(ctx))
        ls = loss_function(out, label.as_in_context(ctx)).mean()
        Ls.append(ls)
        step_loss += sum([L.asscalar() for L in Ls])
        metric.update([label], [out])
    metric_nm, metric_val = metric.get()
    if not isinstance(metric_nm, list):
        metric_nm = [metric_nm]
        metric_val = [metric_val]
    metric_str = 'validation metrics:' + ','.join(
        [i + ':{:.4f}' for i in metric_nm])
    logging.info(metric_str.format(*metric_val))


def train(metric):
    """Training function."""
    optimizer_params_w = {'learning_rate': lr, 'epsilon': 1e-6, 'wd': 0.01}
    optimizer_params_b = {'learning_rate': lr, 'epsilon': 1e-6, 'wd': 0.0}
    try:
        trainer_w = gluon.Trainer(
            model.collect_params('.*weight'),
            args.optimizer,
            optimizer_params_w,
            update_on_kvstore=False)
        trainer_b = gluon.Trainer(
            model.collect_params('.*beta|.*gamma|.*bias'),
            args.optimizer,
            optimizer_params_b,
            update_on_kvstore=False)
    except ValueError as e:
        print(e)
        warnings.warn(
            "AdamW optimizer is not found. Please consider upgrading to "
            "mxnet>=1.5.0. Now the original Adam optimizer is used instead.")
        trainer = gluon.Trainer(
            model.collect_params(),
            'adam',
            optimizer_params,
            update_on_kvstore=False)

    step_size = batch_size * accumulate if accumulate else batch_size
    num_train_steps = int(num_train_examples / step_size * args.epochs)
    warmup_ratio = args.warmup_ratio
    num_warmup_steps = int(num_train_steps * warmup_ratio)
    step_num = 0

    # Collect differentiable parameters
    params = [
        p for p in model.collect_params().values() if p.grad_req != 'null'
    ]
    # Set grad_req if gradient accumulation is required
    if accumulate:
        for p in params:
            p.grad_req = 'add'

    for epoch_id in range(args.epochs):
        metric.reset()
        step_loss = 0
        tic = time.time()
        for batch_id, seqs in enumerate(train_data):
            # set grad to zero for gradient accumulation
            if accumulate:
                if batch_id % accumulate == 0:
                    model.collect_params().zero_grad()
                    step_num += 1
            else:
                step_num += 1
            # learning rate schedule
            if step_num < num_warmup_steps:
                new_lr = lr * step_num / num_warmup_steps
            else:
                offset = (step_num - num_warmup_steps) * lr / (
                    num_train_steps - num_warmup_steps)
                new_lr = lr - offset
            trainer_w.set_learning_rate(new_lr)
            trainer_b.set_learning_rate(new_lr)
            # forward and backward
            with mx.autograd.record():
                input_ids, valid_length, type_ids, label = seqs
                out = model(
                    input_ids.as_in_context(ctx), type_ids.as_in_context(ctx),
                    valid_length.astype('float32').as_in_context(ctx))
                ls = loss_function(out, label.as_in_context(ctx)).mean()
            ls.backward()
            # update
            if not accumulate or (batch_id + 1) % accumulate == 0:
                trainer_w.allreduce_grads()
                trainer_b.allreduce_grads()
                nlp.utils.clip_grad_global_norm(params, 1)
                trainer_w.update(accumulate if accumulate else 1)
                trainer_b.update(accumulate if accumulate else 1)
            step_loss += ls.asscalar()
            metric.update([label], [out])
            if (batch_id + 1) % (args.log_interval) == 0:
                metric_nm, metric_val = metric.get()
                if not isinstance(metric_nm, list):
                    metric_nm = [metric_nm]
                    metric_val = [metric_val]
                eval_str = '[Epoch {} Batch {}/{}] loss={:.4f}, lr={:.7f}, metrics=' + \
                    ','.join([i + ':{:.4f}' for i in metric_nm])
                logging.info(eval_str.format(epoch_id, batch_id + 1, len(train_data), \
                    step_loss / args.log_interval, \
                    trainer_w.learning_rate, *metric_val))
                step_loss = 0
        mx.nd.waitall()
        evaluate(metric)
        toc = time.time()
        logging.info('Time cost={:.1f}s'.format(toc - tic))
        tic = toc


if __name__ == '__main__':
    pool_type = os.environ.get('MXNET_GPU_MEM_POOL_TYPE', '')
    if pool_type.lower() == 'round':
        logging.info(
            'Setting MXNET_GPU_MEM_POOL_TYPE="Round" may lead to higher memory '
            'usage and faster speed. If you encounter OOM errors, please unset '
            'this environment variable.')
    train(tasks[task_name].get_metric())
