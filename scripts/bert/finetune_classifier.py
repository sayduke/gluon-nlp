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

import argparse
import random
import logging
import numpy as np
import mxnet as mx
from mxnet import gluon
from gluonnlp.model import bert_12_768_12
from bert import BERTClassifier, BERTRegression
from tokenizer import FullTokenizer
from dataset import MRPCDataset, QQPDataset, RTEDataset, \
    STSBDataset, ClassificationTransform, RegressionTransform, \
    QNLIDataset, COLADataset, MNLIDataset, WNLIDataset, SSTDataset

np.random.seed(0)
random.seed(0)
mx.random.seed(2)
logging.getLogger().setLevel(logging.DEBUG)

parser = argparse.ArgumentParser(description='BERT sentence pair classification example.'
                                             'We fine-tune the BERT model on MRPC')
parser.add_argument('--epochs', type=int, default=3, help='number of epochs')
parser.add_argument('--batch_size', type=int, default=32,
                    help='Batch size. Number of examples per gpu in a minibatch')
parser.add_argument('--test_batch_size', type=int, default=8, help='Test batch size')
parser.add_argument('--optimizer', type=str, default='adam', help='optimization algorithm')
parser.add_argument('--task_name', type=str, default='MRPC',
                    help='The name of the task to fine-tune.(MRPC,...)')
parser.add_argument('--lr', type=float, default=5e-5, help='Initial learning rate')
parser.add_argument('--warmup_ratio', type=float, default=0.1,
                    help='ratio of warmup steps used in NOAM\'s stepsize schedule')
parser.add_argument('--log_interval', type=int, default=10, help='report interval')
parser.add_argument('--max_len', type=int, default=128, help='Maximum length of the sentence pairs')
parser.add_argument('--gpu', action='store_true', help='whether to use gpu for finetuning')
args = parser.parse_args()
logging.info(args)
batch_size = args.batch_size
test_batch_size = args.test_batch_size
lr = args.lr

ctx = mx.cpu() if not args.gpu else mx.gpu()

dataset = 'book_corpus_wiki_en_uncased'
bert, vocabulary = bert_12_768_12(dataset_name=dataset,
                                  pretrained=True, ctx=ctx, use_pooler=True,
                                  use_decoder=False, use_classifier=False)
do_lower_case = 'uncased' in dataset
tokenizer = FullTokenizer(vocabulary, do_lower_case=do_lower_case)

if args.task_name in ['STS-B']:
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

tasks = {
    'MRPC':MRPCDataset,
    'QQP':QQPDataset,
    'QNLI':QNLIDataset,
    'RTE':RTEDataset,
    'STS-B':STSBDataset,
    'CoLA':COLADataset,
    'MNLI':MNLIDataset,
    'WNLI':WNLIDataset,
    'SST':SSTDataset
    }


task = tasks[args.task_name]

if args.task_name in ['STS-B',]:
    trans = RegressionTransform(tokenizer, args.max_len)
elif args.task_name in ['CoLA', 'SST']:
    trans = ClassificationTransform(tokenizer, task.get_labels(), args.max_len, pair=False)
else:
    trans = ClassificationTransform(tokenizer, task.get_labels(), args.max_len)


if args.task_name == 'MNLI':
    data_train = task('dev_matched').transform(trans)
    data_dev = task('dev_mismatched').transform(trans)
else:
    data_train = task('train').transform(trans)
    data_dev = task('dev').transform(trans)


bert_dataloader = mx.gluon.data.DataLoader(data_train, batch_size=batch_size,
                                           shuffle=True, last_batch='rollover')
bert_dataloader_dev = mx.gluon.data.DataLoader(data_dev, batch_size=test_batch_size,
                                               shuffle=False)

def evaluate(metric):
    """Evaluate the model on validation dataset.
    """
    step_loss = 0
    metric.reset()
    for _, seqs in enumerate(bert_dataloader_dev):
        Ls = []
        input_ids, valid_len, type_ids, label = seqs
        out = model(input_ids.as_in_context(ctx), type_ids.as_in_context(ctx),
                    valid_len.astype('float32').as_in_context(ctx))
        ls = loss_function(out, label.as_in_context(ctx)).mean()
        Ls.append(ls)
        step_loss += sum([L.asscalar() for L in Ls])
        metric.update([label], [out])
    metric_nm, metric_val = metric.get()
    if not isinstance(metric_nm, list):
        metric_nm = [metric_nm,]
        metric_val = [metric_val,]
    metric_str = 'validation metrics:' + ','.join([i + ':{:.4f}' for i in metric_nm])
    logging.info(metric_str.format(*metric_val))


def train(metric):
    """Training function."""
    trainer_w = gluon.Trainer(model.collect_params('.*weight'), args.optimizer, \
                            {'learning_rate': lr, 'epsilon': 1e-9, 'wd':0.01})

    # Do not apply weight decay on LayerNorm and bias terms
    trainer_b = gluon.Trainer(model.collect_params('.*beta|.*gamma|.*bias'), args.optimizer, \
                            {'learning_rate': lr, 'epsilon': 1e-9, 'wd':0.0})

    num_train_examples = len(data_train)
    num_train_steps = int(num_train_examples / batch_size * args.epochs)
    warmup_ratio = args.warmup_ratio
    num_warmup_steps = int(num_train_steps * warmup_ratio)
    step_num = 0
    differentiable_params = []
    for p in model.collect_params().values():
        if p.grad_req != 'null':
            differentiable_params.append(p)

    for epoch_id in range(args.epochs):
        metric.reset()
        step_loss = 0

        for batch_id, seqs in enumerate(bert_dataloader):
            step_num += 1
            if step_num < num_warmup_steps:
                new_lr = lr * step_num / num_warmup_steps
            else:
                offset = (step_num - num_warmup_steps) * lr / (num_train_steps - num_warmup_steps)
                new_lr = lr - offset
            trainer_w.set_learning_rate(new_lr)
            trainer_b.set_learning_rate(new_lr)
            with mx.autograd.record():
                input_ids, valid_length, type_ids, label = seqs
                out = model(input_ids.as_in_context(ctx), type_ids.as_in_context(ctx),
                            valid_length.astype('float32').as_in_context(ctx))
                ls = loss_function(out, label.as_in_context(ctx)).mean()
            ls.backward()
            grads = [p.grad(ctx) for p in differentiable_params]
            gluon.utils.clip_global_norm(grads, 1)
            trainer_w.step(1)
            trainer_b.step(1)
            step_loss += ls.asscalar()
            metric.update([label], [out])
            if (batch_id + 1) % (args.log_interval) == 0:
                metric_nm, metric_val = metric.get()
                if not isinstance(metric_nm, list):
                    metric_nm = [metric_nm,]
                    metric_val = [metric_val,]
                eval_str = '[Epoch {} Batch {}/{}] loss={:.4f}, lr={:.7f}, metrics=' + \
                    ','.join([i + ':{:.4f}' for i in metric_nm])
                logging.info(eval_str.format(epoch_id, batch_id + 1, len(bert_dataloader), \
                    step_loss / args.log_interval, \
                    trainer_w.learning_rate, *metric_val))
                step_loss = 0
        mx.nd.waitall()
        evaluate(metric)

if __name__ == '__main__':
    train(metric=task.get_metric())
