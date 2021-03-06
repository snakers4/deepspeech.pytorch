import math
import torch
import torch.nn as nn
from functools import reduce
import torch.nn.functional as F
from torch.autograd import Variable
from torch.nn.functional import glu
from collections import OrderedDict
from torch.nn.parameter import Parameter
from switchable_norm import SwitchNorm1d
from linknet import (DecoderBlock, TilingBlock)

try:
    from sru import SRU
except:
    print('SRU not installed')

supported_rnns = {
    'lstm': nn.LSTM,
    'rnn': nn.RNN,
    'gru': nn.GRU,
    'sru': None,
    'cnn': None,
    'glu_small': None,
    'glu_large': None,
    'glu_flexible': None,
    'large_cnn': None,
    'cnn_residual': None,
    'cnn_jasper': None,
    'cnn_jasper_2': None,
    'cnn_residual_repeat': None,
    'tds': None,
    'cnn_residual_repeat_sep': None,
    'cnn_residual_repeat_sep_bpe': None,
    'cnn_residual_repeat_sep_down8': None,
    'cnn_inv_bottleneck_repeat_sep_down8': None,
    'cnn_residual_repeat_sep_down8_denoise': None,
    'cnn_residual_repeat_sep_down8_groups8': None,
    'cnn_residual_repeat_sep_down8_groups8_plain_gru': None,
    'cnn_residual_repeat_sep_down8_groups8_attention': None,
    'cnn_residual_repeat_sep_down8_groups8_double_supervision': None,
    'cnn_residual_repeat_sep_down8_groups8_transformer': None,
    'cnn_residual_repeat_sep_down8_groups8_plain_gru_selu_nosc_nobn': None,
    'cnn_residual_repeat_sep_down8_groups8_plain_gru_selu_nobn': None,
    'cnn_residual_repeat_sep_down8_groups16_transformer': None,
    'cnn_residual_repeat_sep_down8_groups12_transformer': None,
    'cnn_residual_repeat_sep_down8_groups12_transformer_variable': None,
    'cnn_residual_repeat_sep_down8_groups16_transformer_variable': None
}
supported_rnns_inv = dict((v, k) for k, v in supported_rnns.items())


class SequenceWise(nn.Module):
    def __init__(self, module):
        """
        Collapses input of dim T*N*H to (T*N)*H, and applies to a module.
        Allows handling of variable sequence lengths and minibatch sizes.
        :param module: Module to apply input to.
        """
        super(SequenceWise, self).__init__()
        self.module = module

    def forward(self, x):
        t, n = x.size(0), x.size(1)
        x = x.view(t * n, -1)
        x = self.module(x)
        x = x.view(t, n, -1)
        return x

    def __repr__(self):
        tmpstr = self.__class__.__name__ + ' (\n'
        tmpstr += self.module.__repr__()
        tmpstr += ')'
        return tmpstr


class MaskConv(nn.Module):
    def __init__(self, seq_module):
        """
        Adds padding to the output of the module based on the given lengths. This is to ensure that the
        results of the model do not change when batch sizes change during inference.
        Input needs to be in the shape of (BxCxDxT)
        :param seq_module: The sequential module containing the conv stack.
        """
        super(MaskConv, self).__init__()
        self.seq_module = seq_module

    def forward(self, x, lengths):
        """
        :param x: The input of size BxCxDxT
        :param lengths: The actual length of each sequence in the batch
        :return: Masked output from the module
        """
        for module in self.seq_module:
            x = module(x)
            mask = torch.ByteTensor(x.size()).fill_(0)
            if x.is_cuda:
                mask = mask.cuda()
            for i, length in enumerate(lengths):
                length = length.item()
                if (mask[i].size(2) - length) > 0:
                    mask[i].narrow(2, length, mask[i].size(2) - length).fill_(1)
            x = x.masked_fill(mask, 0)
        return x, lengths


class BatchRNN(nn.Module):
    def __init__(self, input_size, hidden_size,
                 rnn_type=nn.LSTM, bidirectional=False, batch_norm=True, bnm=0.1,
                 batch_first=False):
        super(BatchRNN, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.bidirectional = bidirectional
        self.bnm = bnm
        self.batch_norm = SequenceWise(nn.BatchNorm1d(input_size, momentum=bnm)) if batch_norm else None
        if rnn_type == 'sru':
            self.sru = True
            self.rnn = SRU(input_size, hidden_size,
                           bidirectional=bidirectional, rescale=True)
        else:
            self.sru = False
            self.rnn = rnn_type(input_size=input_size, hidden_size=hidden_size,
                                bidirectional=bidirectional, bias=True,
                                batch_first=batch_first)
        self.num_directions = 2 if bidirectional else 1

    def flatten_parameters(self):
        self.rnn.flatten_parameters()

    def forward(self, x, output_lengths=None):
        assert x.is_cuda
        if output_lengths is not None:
            # legacy case
            max_seq_length = x.size(0)
            if self.batch_norm is not None:
                x = self.batch_norm(x)
                # x = x._replace(data=self.batch_norm(x.data))
            if not self.sru:
                x = nn.utils.rnn.pack_padded_sequence(x, output_lengths.data.cpu().numpy())
            x, h = self.rnn(x)
            if not self.sru:
                x, _ = nn.utils.rnn.pad_packed_sequence(x, total_length=max_seq_length)
            if self.bidirectional:
                x = x.view(x.size(0), x.size(1), 2, -1).sum(2).view(x.size(0), x.size(1), -1)  # (TxNxH*2) -> (TxNxH) by sum
            # x = x.to('cuda')
        else:
            # in case of CNN encoder
            # all sequences are already of the same lengths
            # and 8 times shorter, so this code does not matter as much
            if self.batch_norm is not None:
                x = self.batch_norm(x)
            x, h = self.rnn(x)
            if self.bidirectional:
                x = x.view(x.size(0),
                           x.size(1),
                           2, -1).sum(2).view(x.size(0),
                                              x.size(1),
                                              -1)  # (TxNxH*2) -> (TxNxH) by sum
        return x


class DeepBatchRNN(nn.Module):
    def __init__(self, input_size, hidden_size, rnn_type=nn.LSTM, bidirectional=False, num_layers=1,
                 batch_norm=True, sum_directions=True, **kwargs):
        super(DeepBatchRNN, self).__init__()
        self._bidirectional = bidirectional
        rnns = []
        rnn = BatchRNN(input_size=input_size, hidden_size=hidden_size, rnn_type=rnn_type, bidirectional=bidirectional,
                       batch_norm=False)
        rnns.append(('0', rnn))
        for x in range(num_layers - 1):
            rnn = BatchRNN(input_size=hidden_size, hidden_size=hidden_size, rnn_type=rnn_type,
                           bidirectional=bidirectional, batch_norm=batch_norm)
            rnns.append(('%d' % (x + 1), rnn))
        self.rnns = nn.Sequential(OrderedDict(rnns))
        self.sum_directions = sum_directions

    def flatten_parameters(self):
        for x in range(len(self.rnns)):
            self.rnns[x].flatten_parameters()

    def forward(self, x, lengths):
        max_seq_length = x.size(0)
        x = nn.utils.rnn.pack_padded_sequence(x, lengths.data.squeeze(0).cpu().numpy())
        x = self.rnns(x)
        x, _ = nn.utils.rnn.pad_packed_sequence(x, total_length=max_seq_length)
        return x, None


class Lookahead(nn.Module):
    # Wang et al 2016 - Lookahead Convolution Layer for Unidirectional Recurrent Neural Networks
    # input shape - sequence, batch, feature - TxNxH
    # output shape - same as input
    def __init__(self, n_features, context):
        # should we handle batch_first=True?
        super(Lookahead, self).__init__()
        self.n_features = n_features
        self.weight = Parameter(torch.Tensor(n_features, context + 1))
        assert context > 0
        self.context = context
        self.register_parameter('bias', None)
        self.init_parameters()

    def init_parameters(self):  # what's a better way initialiase this layer?
        stdv = 1. / math.sqrt(self.weight.size(1))
        self.weight.data.uniform_(-stdv, stdv)

    def forward(self, input):
        seq_len = input.size(0)
        # pad the 0th dimension (T/sequence) with zeroes whose number = context
        # Once pytorch's padding functions have settled, should move to those.
        padding = torch.zeros(self.context, *(input.size()[1:])).type_as(input.data)
        x = torch.cat((input, Variable(padding)), 0)

        # add lookahead windows (with context+1 width) as a fourth dimension
        # for each seq-batch-feature combination
        x = [x[i:i + self.context + 1] for i in range(seq_len)]  # TxLxNxH - sequence, context, batch, feature
        x = torch.stack(x)
        x = x.permute(0, 2, 3, 1)  # TxNxHxL - sequence, batch, feature, context

        x = torch.mul(x, self.weight).sum(dim=3)
        return x

    def __repr__(self):
        return self.__class__.__name__ + '(' \
               + 'n_features=' + str(self.n_features) \
               + ', context=' + str(self.context) + ')'


DEBUG = 0


class DeepSpeech(nn.Module):
    def __init__(self, rnn_type=nn.LSTM, labels="abc", rnn_hidden_size=768,
                 nb_layers=6,
                 audio_conf=None,
                 bidirectional=True, context=20, bnm=0.1,
                 kernel_size=7,
                 dropout=0, cnn_width=256,
                 phoneme_count=0, decoder_layers=4, decoder_girth=1):
        super(DeepSpeech, self).__init__()

        # model metadata needed for serialization/deserialization
        if audio_conf is None:
            audio_conf = {}
        self._version = '0.0.1'
        self._hidden_size = rnn_hidden_size
        self._hidden_layers = nb_layers
        self._rnn_type = rnn_type
        self._audio_conf = audio_conf or {}
        self._labels = labels
        self._bidirectional = bidirectional
        self._bnm = bnm
        self._dropout = dropout
        self._cnn_width = cnn_width
        self._decoder_layers = decoder_layers
        self._kernel_size = kernel_size
        self._decoder_girth = decoder_girth

        if phoneme_count > 0:
            self._phoneme_count = phoneme_count

        sample_rate = self._audio_conf.get("sample_rate", 16000)
        window_size = self._audio_conf.get("window_size", 0.02)
        num_classes = len(self._labels)

        if self._rnn_type not in ['tds',
                                  'cnn_residual_repeat_sep_down8_groups8_plain_gru']:
            self.dropout1 = nn.Dropout(p=0.1, inplace=True)
            self.conv = MaskConv(nn.Sequential(
                nn.Conv2d(1, 32, kernel_size=(41, 11), stride=(2, 2), padding=(20, 5)),
                nn.BatchNorm2d(32, momentum=bnm),
                nn.Hardtanh(0, 20, inplace=True),
                nn.Conv2d(32, 32, kernel_size=(21, 11), stride=(2, 1), padding=(10, 5)),
                nn.BatchNorm2d(32, momentum=bnm),
                nn.Hardtanh(0, 20, inplace=True),
            ))

        if self._rnn_type == 'cnn':  #  wav2letter with some features
            size = rnn_hidden_size
            modules = Wav2Letter(
                DotDict({
                    'size':size, # here it defines model epilog size
                    'bnorm':True,
                    'bnm':self._bnm,
                    'dropout':dropout,
                    'cnn_width':self._cnn_width, # cnn filters
                    'not_glu':self._bidirectional, # glu or basic relu
                    'repeat_layers':self._hidden_layers, # depth, only middle part
                    'kernel_size':13
                })
            )
            self.rnns = nn.Sequential(*modules)
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'cnn_residual':  #  wav2letter with some features
            size = rnn_hidden_size
            self.rnns = ResidualWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.25,
                    'skip': True
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
            # make checkpoints reverse compatible
            if hasattr(self, '_phoneme_count'):
                self.fc_phoneme = nn.Sequential(
                    nn.Conv1d(in_channels=size,
                              out_channels=self._phoneme_count, kernel_size=1)
                )
        elif self._rnn_type == 'cnn_residual_repeat':  # repeat middle convs
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
            # make checkpoints reverse compatible
            if hasattr(self, '_phoneme_count'):
                self.fc_phoneme = nn.Sequential(
                    nn.Conv1d(in_channels=size,
                              out_channels=self._phoneme_count, kernel_size=1)
                )
        elif self._rnn_type == 'cnn_residual_repeat_sep':  # add sep convs
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable':True,
                    'dilated_blocks': [0]
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
            # make checkpoints reverse compatible
            if hasattr(self, '_phoneme_count'):
                self.fc_phoneme = nn.Sequential(
                    nn.Conv1d(in_channels=size,
                              out_channels=self._phoneme_count, kernel_size=1)
                )
        elif self._rnn_type == 'cnn_residual_repeat_sep_bpe':  # add scale 4
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 2,
                    'dilated_blocks': [0]
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
            # make checkpoints reverse compatible
            if hasattr(self, '_phoneme_count'):
                self.fc_phoneme = nn.Sequential(
                    nn.Conv1d(in_channels=size,
                              out_channels=self._phoneme_count, kernel_size=1)
                )
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8':  # add scale 8
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 12  # optimal group count, 768 // 12 = 64
                })
            )
            # https://arxiv.org/abs/1905.09788
            if False:
                self.fc = MultiSampleFC(heads=8,
                                        dropout_rate=dropout,
                                        in_channels=size,
                                        out_channels=num_classes)
            else:
                self.fc = nn.Sequential(
                    nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
                )
            # make checkpoints reverse compatible
            if hasattr(self, '_phoneme_count'):
                self.fc_phoneme = nn.Sequential(
                    nn.Conv1d(in_channels=size,
                              out_channels=self._phoneme_count, kernel_size=1)
                )
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_groups8':  # add scale 8
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 8  # optimal group count, 512 // 12 = 64
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_groups8_plain_gru':  # add scale 8
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 8,  # optimal group count, 512 // 12 = 64
                    'decoder_type': 'plain_gru'
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_groups8_transformer':  # add scale 8
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 8,  # optimal group count, 512 // 12 = 64
                    'decoder_type': 'transformer',
                    'decoder_layers': self._decoder_layers,
                    'decoder_girth': self._decoder_girth,
                    'vary_cnn_width': False
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_groups12_transformer':  # add scale 8
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 12,  # optimal group count, 1024 // 16 = 64
                    'decoder_type': 'transformer',
                    'decoder_layers': self._decoder_layers,
                    'vary_cnn_width': False
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )            
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_groups16_transformer':  # add scale 8
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 16,  # optimal group count, 1024 // 16 = 64
                    'decoder_type': 'transformer',
                    'decoder_layers': self._decoder_layers,
                    'vary_cnn_width': False
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_groups12_transformer_variable':  # add scale 8
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 12,  # optimal group count, 1024 // 16 = 64
                    'decoder_type': 'transformer',
                    'decoder_layers': self._decoder_layers,
                    'vary_cnn_width': True
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            ) 
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_groups16_transformer_variable':  # add scale 8
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 16,  # optimal group count, 1024 // 16 = 64
                    'decoder_type': 'transformer',
                    'decoder_layers': self._decoder_layers,
                    'vary_cnn_width': True
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )             
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_groups8_plain_gru_selu_nosc_nobn':  # add scale 8
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': False,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': False,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 8,  # optimal group count, 512 // 12 = 64
                    'decoder_type': 'plain_gru',
                    'nonlinearity': nn.SELU(inplace=True)
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_groups8_plain_gru_selu_nobn':  # add scale 8
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': False,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 8,  # optimal group count, 512 // 12 = 64
                    'decoder_type': 'plain_gru',
                    'nonlinearity': nn.SELU(inplace=True)
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_groups8_attention':
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 8,  # optimal group count, 512 // 12 = 64
                    'decoder_type': 'attention',
                    'num_classes': num_classes  # sos and eos are already included, pad is blank token
                })
            )
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_groups8_double_supervision':
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation
                    'groups': 8,  # optimal group count, 512 // 12 = 64
                    'decoder_type': 'double_supervision',
                    'num_classes': num_classes  # sos and eos are already included, pad is blank token
                })
            )
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_denoise':  # add scale 8
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [], # no dilation,
                    'denoise': True
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
            # make checkpoints reverse compatible
            if hasattr(self, '_phoneme_count'):
                self.fc_phoneme = nn.Sequential(
                    nn.Conv1d(in_channels=size,
                              out_channels=self._phoneme_count, kernel_size=1)
                )
        elif self._rnn_type == 'cnn_inv_bottleneck_repeat_sep_down8':  # add inverted bottleneck
            size = rnn_hidden_size
            self.rnns = ResidualRepeatWav2Letter(
                DotDict({
                    'size': rnn_hidden_size,  # here it defines model epilog size
                    'bnorm': True,
                    'bnm': self._bnm,
                    'dropout': dropout,
                    'cnn_width': self._cnn_width,  # cnn filters
                    'not_glu': self._bidirectional,  # glu or basic relu
                    'repeat_layers': self._hidden_layers,  # depth, only middle part
                    'kernel_size': self._kernel_size,
                    'se_ratio': 0.2,
                    'skip': True,
                    'separable': True,
                    'add_downsample': 4,
                    'dilated_blocks': [],  # no dilation because of scale 8
                    'inverted_bottleneck': True
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
            # make checkpoints reverse compatible
            if hasattr(self, '_phoneme_count'):
                self.fc_phoneme = nn.Sequential(
                    nn.Conv1d(in_channels=size,
                              out_channels=self._phoneme_count, kernel_size=1)
                )
        elif self._rnn_type == 'tds':  # repeat middle convs
            # TDS config
            size = rnn_hidden_size
            input_channels = 161
            h = 81
            kernel_size = 21
            blocks = 3
            strides = [2, 2, 1]
            repeats = [2, 3, 6]
            channels = [10, 14, 18]

            self.rnns = TDS(
                DotDict({
                    'dropout': dropout,
                    'h': h,
                    'kernel_size': kernel_size,
                    'blocks': blocks,
                    'strides': strides,
                    'repeats': repeats,
                    'channels': channels,
                    'output_size': size,
                    'input_channels': input_channels
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
            # make checkpoints reverse compatible
            if hasattr(self, '_phoneme_count'):
                self.fc_phoneme = nn.Sequential(
                    nn.Conv1d(in_channels=size,
                              out_channels=self._phoneme_count, kernel_size=1)
                )
        elif self._rnn_type == 'cnn_jasper':  #  http://arxiv.org/abs/1904.03288
            size = 1024
            big_block_repeat = self._cnn_width // 5
            jasper_config = {
                'dense_residual':False,
                'input_channels':161,
                'bn_momentum':0.1,
                'bn_eps':1e-05,
                'activation_fn':nn.ReLU,
                'repeats':[1] + [self._hidden_layers] * self._cnn_width + [1,1],
                'channels':[256] + sorted(big_block_repeat * [256,384,512,640,768]) + [896,1024],
                'kernel_sizes':[11] + sorted(big_block_repeat * [11,13,17,21,25]) + [29,1],
                'strides':[2] + [1] * self._cnn_width + [1,1],
                'dilations':[1] + [1] * self._cnn_width + [2,1],
                'dropouts':[0.2] + sorted(big_block_repeat * [0.2,0.2,0.2,0.3,0.3]) + [0.4,0.4],
                'residual':[0] + [1] * self._cnn_width + [0,0],
            }
            print(jasper_config)
            self.rnns = JasperNet(jasper_config)
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'cnn_jasper_2':  #  http://arxiv.org/abs/1904.03288
            size = 1024
            jasper_config = {
                'input_channels':161,
                'bn_momentum':0.1,
                'bn_eps':1e-05,
                'activation_fn':nn.ReLU,
                'repeats':self._hidden_layers,
                'num_modules':self._cnn_width

            }
            print(jasper_config)
            self.rnns = JasperNetEasy(jasper_config)
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'large_cnn':
            self.rnns = LargeCNN(
                DotDict({
                    'input_channels':161,
                    'bnm':bnm,
                    'dropout':dropout,
                })
            )
            # last GLU layer size
            size = self.rnns.last_channels
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'glu_small':
            self.rnns = SmallGLU(
                DotDict({
                    'input_channels':161,
                    'layer_num':self._hidden_layers,
                    'bnm':bnm,
                    'dropout':dropout,
                })
            )
            # last GLU layer size
            size = self.rnns.last_channels
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'glu_large':
            self.rnns = LargeGLU(
                DotDict({
                    'input_channels':161
                })
            )
            self.fc = nn.Sequential(
                nn.Conv1d(in_channels=size, out_channels=num_classes, kernel_size=1)
            )
        elif self._rnn_type == 'glu_flexible':
            raise NotImplementedError("Customizable GLU not yet implemented")
        else:  # original ds2
            # Based on above convolutions and spectrogram size using conv formula (W - F + 2P)/ S+1
            rnn_input_size = int(math.floor((sample_rate * window_size + 1e-2) / 2) + 1)
            rnn_input_size = int(math.floor(rnn_input_size + 2 * 20 - 41 + 1e-2) / 2 + 1)
            rnn_input_size = int(math.floor(rnn_input_size + 2 * 10 - 21 + 1e-2) / 2 + 1)
            rnn_input_size *= 32

            rnns = []
            if rnn_type == 'sru':
                pass
            else:
                rnn_type = supported_rnns[rnn_type]

            rnn = BatchRNN(input_size=rnn_input_size, hidden_size=rnn_hidden_size, rnn_type=rnn_type,
                           bidirectional=bidirectional, batch_norm=False)
            rnns.append(('0', rnn))
            for x in range(nb_layers - 1):
                rnn = BatchRNN(input_size=rnn_hidden_size, hidden_size=rnn_hidden_size,
                               rnn_type=rnn_type,
                               bidirectional=bidirectional, bnm=bnm)
                rnns.append(('%d' % (x + 1), rnn))
            self.rnns = nn.Sequential(OrderedDict(rnns))

            self.lookahead = nn.Sequential(
                # consider adding batch norm?
                Lookahead(rnn_hidden_size, context=context),
                nn.Hardtanh(0, 20, inplace=True)
            ) if not bidirectional else None

            fully_connected = nn.Sequential(
                nn.BatchNorm1d(rnn_hidden_size, momentum=bnm),
                nn.Linear(rnn_hidden_size, num_classes, bias=False)
            )
            self.fc = nn.Sequential(
                SequenceWise(fully_connected),
            )

    def forward(self, x, lengths=None,
                trg=None):
        # assert x.is_cuda
        if DEBUG: print(lengths)
        lengths = lengths.cpu().int()
        if DEBUG: print(lengths)
        if DEBUG:
            output_lengths = self.get_seq_lens(lengths)
            print('Projected output lengths {}'.format(output_lengths))
        else:
            output_lengths = self.get_seq_lens(lengths).cuda()

        if self._rnn_type in ['cnn', 'glu_small', 'glu_large', 'large_cnn',
                              'cnn_residual', 'cnn_jasper', 'cnn_jasper_2',
                              'cnn_residual_repeat', 'tds','cnn_residual_repeat_sep',
                              'cnn_residual_repeat_sep_bpe', 'cnn_residual_repeat_sep_down8',
                              'cnn_inv_bottleneck_repeat_sep_down8', 'cnn_residual_repeat_sep_down8_groups8',
                              'cnn_residual_repeat_sep_down8_groups8_plain_gru',
                              'cnn_residual_repeat_sep_down8_groups8_transformer',
                              'cnn_residual_repeat_sep_down8_groups16_transformer',
                              'cnn_residual_repeat_sep_down8_groups12_transformer',
                              'cnn_residual_repeat_sep_down8_groups12_transformer_variable',
                              'cnn_residual_repeat_sep_down8_groups16_transformer_variable',
                              'cnn_residual_repeat_sep_down8_groups8_plain_gru_selu_nosc_nobn',
                              'cnn_residual_repeat_sep_down8_groups8_plain_gru_selu_nobn']:
            x = x.squeeze(1)
            x = self.rnns(x)
            if hasattr(self, '_phoneme_count'):
                x_phoneme = self.fc_phoneme(x)
                x_phoneme = x_phoneme.transpose(1, 2).transpose(0, 1).contiguous()
            x = self.fc(x)
            x = x.transpose(1, 2).transpose(0, 1).contiguous()
        elif self._rnn_type in ['cnn_residual_repeat_sep_down8_groups8_attention']:
            x = x.squeeze(1)
            x = self.rnns(x, trg=trg)
            # just return the result, all processing is done inside
            # no difference between softmax / wo softmax
            return x, output_lengths
        elif self._rnn_type in ['cnn_residual_repeat_sep_down8_groups8_double_supervision']:
            x = x.squeeze(1)
            ctc_out, s2s_out = self.rnns(x, trg=trg)
            # just return the result, all processing is done inside
            # no difference between softmax / wo softmax
            return ctc_out, s2s_out, output_lengths
        elif self._rnn_type == 'cnn_residual_repeat_sep_down8_denoise':
            x = x.squeeze(1)
            x, denoise_mask = self.rnns(x)
            # print(denoise_mask.size())
            x = self.fc(x)
            x = x.transpose(1, 2).transpose(0, 1).contiguous()
        else:
            # x = self.dropout1(x)
            x, _ = self.conv(x, output_lengths)
            # x = self.dropout2(x)
            if DEBUG: assert x.is_cuda
            # x = x.to('cuda')
            sizes = x.size()
            x = x.view(sizes[0], sizes[1] * sizes[2], sizes[3])  # Collapse feature dimension
            x = x.transpose(1, 2).transpose(0, 1).contiguous()  # TxNxH
            # assert x.is_cuda

            for rnn in self.rnns:
                x = rnn(x, output_lengths)
                # assert x.is_cuda

            if not self._bidirectional:  # no need for lookahead layer in bidirectional
                x = self.lookahead(x)
                # assert x.is_cuda

            x = self.fc(x)
        # if not DEBUG: assert x.is_cuda
        x = x.transpose(0, 1)
        # identity in training mode, softmax in eval mode
        outs = F.softmax(x, dim=-1)
        # if not DEBUG: assert outs.is_cuda
        # if not DEBUG: assert output_lengths.is_cuda

        if hasattr(self, '_phoneme_count'):
            x_phoneme = x_phoneme.transpose(0, 1)
            outs_phoneme = F.softmax(x_phoneme, dim=-1)
            # phoneme outputs will have the same length
            return x, outs, output_lengths, x_phoneme, outs_phoneme
        else:
            # print(output_lengths, x.size())
            if self._rnn_type == 'cnn_residual_repeat_sep_down8_denoise':
                return x, outs, output_lengths, denoise_mask
            else:
                return x, outs, output_lengths

    def get_seq_lens(self, input_length):
        """
        Given a 1D Tensor or Variable containing integer sequence lengths, return a 1D tensor or variable
        containing the size sequences that will be output by the network.
        :param input_length: 1D Tensor
        :return: 1D Tensor scaled by model
        """
        seq_len = input_length
        if self._rnn_type in ['cnn_residual_repeat_sep_bpe',
                              'cnn_residual_repeat_sep_down8',
                              'cnn_inv_bottleneck_repeat_sep_down8',
                              'cnn_residual_repeat_sep_down8_groups8',
                              'cnn_residual_repeat_sep_down8_groups8_plain_gru',
                              'cnn_residual_repeat_sep_down8_groups8_attention',
                              'cnn_residual_repeat_sep_down8_groups8_double_supervision',
                              'cnn_residual_repeat_sep_down8_groups8_transformer',
                              'cnn_residual_repeat_sep_down8_groups16_transformer',
                              'cnn_residual_repeat_sep_down8_groups12_transformer',
                              'cnn_residual_repeat_sep_down8_groups12_transformer_variable',
                              'cnn_residual_repeat_sep_down8_groups16_transformer_variable',
                              'cnn_residual_repeat_sep_down8_groups8_plain_gru_selu_nosc_nobn',
                              'cnn_residual_repeat_sep_down8_groups8_plain_gru_selu_nobn']:
            for m in self.rnns.modules():
                if type(m) == nn.modules.conv.Conv1d:
                    seq_len = ((seq_len + 2 * m.padding[0] - m.dilation[0] * (m.kernel_size[0] - 1) - 1) / m.stride[0] + 1)
        elif self._rnn_type in ['cnn_residual_repeat_sep_down8_denoise']:
            for m in self.rnns.encoder.modules():
                if type(m) == nn.modules.conv.Conv1d:
                    seq_len = ((seq_len + 2 * m.padding[0] - m.dilation[0] * (m.kernel_size[0] - 1) - 1) / m.stride[0] + 1)
        elif self._rnn_type not in ['tds']:
            for m in self.conv.modules():
                if type(m) == nn.modules.conv.Conv2d:
                    seq_len = ((seq_len + 2 * m.padding[1] - m.dilation[1] * (m.kernel_size[1] - 1) - 1) / m.stride[1] + 1)
        elif self._rnn_type == 'tds':
            # all convolutions are forced to be same
            seq_len = (seq_len + 0.01) // reduce(lambda x, y: x*y, self.rnns.strides)
        else:
            raise NotImplementedError()
        return seq_len.int()

    @classmethod
    def load_model(cls, path):
        package = torch.load(path, map_location=lambda storage, loc: storage)
        model = cls(rnn_hidden_size=package['hidden_size'],
                    nb_layers=package['hidden_layers'],
                    labels=package['labels'],
                    audio_conf=package['audio_conf'],
                    rnn_type=package['rnn_type'],
                    bnm=package.get('bnm', 0.1),
                    bidirectional=package.get('bidirectional', True),
                    decoder_layers=package.get('decoder_layers', 4))
        model.load_state_dict(package['state_dict'])
        if package['rnn_type'] != 'cnn':
            for x in model.rnns:
                x.flatten_parameters()
        return model

    @classmethod
    def load_model_package(cls, package):
        kwargs = {
            'rnn_hidden_size': package['hidden_size'],
            'nb_layers': package['hidden_layers'],
            'labels': package['labels'],
            'audio_conf': package['audio_conf'],
            'rnn_type': package['rnn_type'],
            'bnm': package.get('bnm', 0.1),
            'bidirectional': package.get('bidirectional', True),
            'dropout': package.get('dropout', 0),
            'cnn_width': package.get('cnn_width', 0),
            'phoneme_count': package.get('phoneme_count', 0),
            'decoder_layers': package.get('decoder_layers', 4),
            'kernel_size': package.get('kernel_size', 7),
            'decoder_girth': package.get('decoder_girth', 1),
        }
        model = cls(**kwargs)
        model.load_state_dict(package['state_dict'])
        return model

    @staticmethod
    def add_phonemes_to_model(model,
                              phoneme_count=0):
        '''Add phonemes to an already pre-trained model
        '''
        model._phoneme_count = phoneme_count
        model.fc_phoneme = nn.Sequential(
            nn.Conv1d(in_channels=model._hidden_size,
                      out_channels=model._phoneme_count,
                      kernel_size=1)
        )
        return model

    @staticmethod
    def add_denoising_to_model(model):
        '''Turn a pre-trained model into a model with denoising layer
        '''
        assert model._rnn_type == 'cnn_residual_repeat_sep_down8'
        model._rnn_type = 'cnn_residual_repeat_sep_down8_denoise'
        repeat_layers = 12
        cnn_width = 768
        model.rnns.encoder = nn.ModuleDict({
            'conv1':      nn.Sequential(*model.rnns.layers[                                 : 1 + 1 * (repeat_layers // 3) + 0]),
            'conv2':      nn.Sequential(*model.rnns.layers[1 + 1 * (repeat_layers // 3)     : 1 + 2 * (repeat_layers // 3) + 1]),
            'conv3':      nn.Sequential(*model.rnns.layers[1 + 2 * (repeat_layers // 3) + 1 : 1 + 3 * (repeat_layers // 3) + 2]),
            'final_conv': nn.Sequential(*model.rnns.layers[1 + 3 * (repeat_layers // 3) + 2 : ]),
        })
        model.rnns.denoise = LinkNetDenoising(filters=[161]+[cnn_width]*3)
        del model.rnns.layers

        if True:
            for block in [model.rnns.encoder]:
                for p in block.parameters():
                    p.requires_grad = False
        print('Gradients disabled for the encoder')

        return model

    @staticmethod
    def add_s2s_decoder_to_model(model,
                                 labels,
                                 decoder_layers=2,
                                 dropout=0.1,):
        '''Turn a pre-trained model into a model with a s2s decoder
        '''
        # Transform a pre-trained model with GRU
        assert model._rnn_type == 'cnn_residual_repeat_sep_down8_groups8_plain_gru'
        model._rnn_type = 'cnn_residual_repeat_sep_down8_groups8_attention'
        model._labels = labels

        num_classes = len(labels)
        model.rnns.num_classes = num_classes
        model.rnns.decoder_type = 'attention'

        attention = BahdanauAttention(model._hidden_size,
                                      query_size=model._hidden_size)
        model.rnns.decoder = Decoder(256, model._hidden_size,
                                     num_classes,
                                     attention,
                                     num_layers=decoder_layers,
                                     dropout=dropout,
                                     sos_index=num_classes-2)
        return model

    @staticmethod
    def serialize(model, optimizer=None, epoch=None, iteration=None, loss_results=None, checkpoint=None,
                  cer_results=None, wer_results=None, avg_loss=None, meta=None,
                  checkpoint_cer_results=None, checkpoint_wer_results=None, checkpoint_loss_results=None,
                  trainval_checkpoint_loss_results=None, trainval_checkpoint_cer_results=None, trainval_checkpoint_wer_results=None):
        model = model.module if DeepSpeech.is_parallel(model) else model
        package = {
            'version': model._version,
            'hidden_size': model._hidden_size,
            'hidden_layers': model._hidden_layers,
            'rnn_type': model._rnn_type,
            'audio_conf': model._audio_conf,
            'labels': model._labels,
            'state_dict': model.state_dict(),
            'bnm': model._bnm,
            'bidirectional': model._bidirectional,
            'dropout': model._dropout,
            'cnn_width': model._cnn_width,
            'decoder_layers': model._decoder_layers,
            'kernel_size': model._kernel_size,
            'decoder_girth': model._decoder_girth,
        }
        if hasattr(model, '_phoneme_count'):
            package['phoneme_count'] = model._phoneme_count
        if optimizer is not None:
            package['optim_dict'] = optimizer.state_dict()
        if avg_loss is not None:
            package['avg_loss'] = avg_loss
        if epoch is not None:
            package['epoch'] = epoch + 1  # increment for readability
        if iteration is not None:
            package['iteration'] = iteration
        package['checkpoint'] = checkpoint
        if loss_results is not None:
            package['loss_results'] = loss_results
            package['cer_results'] = cer_results
            package['wer_results'] = wer_results
            package['checkpoint_cer_results'] = checkpoint_cer_results
            package['checkpoint_wer_results'] = checkpoint_wer_results
            package['checkpoint_loss_results'] = checkpoint_loss_results
            # only if the relevant flag passed to args in train.py
            # otherwise always None
            package['trainval_checkpoint_loss_results'] = trainval_checkpoint_loss_results
            package['trainval_checkpoint_cer_results'] = trainval_checkpoint_cer_results
            package['trainval_checkpoint_wer_results'] = trainval_checkpoint_wer_results
        if meta is not None:
            package['meta'] = meta
        return package

    @staticmethod
    def get_labels(model):
        return model.module._labels if model.is_parallel(model) else model._labels

    @staticmethod
    def get_param_size(model):
        params = 0
        for p in model.parameters():
            tmp = 1
            for x in p.size():
                tmp *= x
            params += tmp
        return params

    @staticmethod
    def get_audio_conf(model):
        return model.module._audio_conf if DeepSpeech.is_parallel(model) else model._audio_conf

    @staticmethod
    def get_meta(model):
        m = model.module if DeepSpeech.is_parallel(model) else model
        meta = {
            "version": m._version,
            "hidden_size": m._hidden_size,
            "hidden_layers": m._hidden_layers,
            "rnn_type": m._rnn_type
        }
        return meta

    @staticmethod
    def is_parallel(model):
        return isinstance(model, torch.nn.parallel.DataParallel) or \
               isinstance(model, torch.nn.parallel.DistributedDataParallel)


# bit ugly, but we need to clean things up!
def Wav2Letter(config):
    assert type(config)==DotDict
    not_glu = config.not_glu
    bnm = config.bnm
    def _block(in_channels, out_channels, kernel_size,
               padding=0, stride=1, bnorm=False, bias=True,
               dropout=0):
        # use self._bidirectional flag as a flag for GLU usage in the CNN
        # the flag is True by default, so use False
        if not not_glu:
            out_channels = int(out_channels * 2)

        res = [nn.Conv1d(in_channels=in_channels, out_channels=out_channels,
                         kernel_size=kernel_size, padding=padding,
                         stride=stride, bias=bias)]
        # for non GLU networks
        if not_glu:
            if bnorm:
                res.append(nn.BatchNorm1d(out_channels, momentum=bnm))
        # use self._bidirectional flag as a flag for GLU usage in the CNN
        if not_glu:
            res.append(nn.ReLU(inplace=True))
        else:
            res.append(GLUModule(dim=1))
        # for GLU networks
        if not not_glu:
            if bnorm:
                res.append(nn.BatchNorm1d(int(out_channels//2),
                                          momentum=bnm))
        if dropout>0:
            res.append(nn.Dropout(dropout))
        return res

    size = config.size
    cnn_width = config.cnn_width
    bnorm = config.bnorm
    dropout = config.dropout
    repeat_layers = config.repeat_layers
    kernel_size = config.kernel_size # wav2letter default - 7
    padding = kernel_size // 2

    # "prolog"
    modules = _block(in_channels=161, out_channels=cnn_width, kernel_size=kernel_size,
                     padding=padding, stride=2, bnorm=bnorm, bias=not bnorm, dropout=dropout)

    # main convs
    for _ in range(0,repeat_layers):
        modules.extend(
            [*_block(in_channels=cnn_width, out_channels=cnn_width, kernel_size=kernel_size,
                     padding=padding, bnorm=bnorm, bias=not bnorm, dropout=dropout)]
        )
    # "epilog"
    modules.extend([*_block(in_channels=cnn_width, out_channels=size, kernel_size=31,
                            padding=15, bnorm=bnorm, bias=not bnorm, dropout=dropout)])
    modules.extend([*_block(in_channels=size, out_channels=size, kernel_size=1,
                            bnorm=bnorm, bias=not bnorm, dropout=dropout)])
    return modules


class ResidualWav2Letter(nn.Module):
    def __init__(self,config):
        super(ResidualWav2Letter, self).__init__()

        size = config.size
        cnn_width = config.cnn_width
        bnorm = config.bnorm
        bnm = config.bnm
        dropout = config.dropout
        repeat_layers = config.repeat_layers
        kernel_size = config.kernel_size # wav2letter default - 7
        padding = kernel_size // 2
        se_ratio = config.se_ratio
        skip = config.skip

        # "prolog"
        modules = [ResCNNBlock(_in=161, out=cnn_width, kernel_size=kernel_size,
                               padding=padding, stride=2,bnm=bnm, bias=not bnorm, dropout=dropout,
                               nonlinearity=nn.ReLU(inplace=True),
                               se_ratio=0,skip=False)] # no skips and attention

        # main convs
        for _ in range(0,repeat_layers):
            modules.extend(
                [ResCNNBlock(_in=cnn_width, out=cnn_width, kernel_size=kernel_size,
                             padding=padding, stride=1,bnm=bnm, bias=not bnorm, dropout=dropout,
                             nonlinearity=nn.ReLU(inplace=True),
                             se_ratio=se_ratio,skip=skip)]
            )
        # "epilog"
        modules.extend([ResCNNBlock(_in=cnn_width, out=size, kernel_size=31,
                                    padding=15, stride=1,bnm=bnm, bias=not bnorm, dropout=dropout,
                                    nonlinearity=nn.ReLU(inplace=True),
                                    se_ratio=0,skip=False)]) # no skips and attention
        modules.extend([ResCNNBlock(_in=size, out=size, kernel_size=1,
                                    padding=0, stride=1,bnm=bnm, bias=not bnorm, dropout=dropout,
                                    nonlinearity=nn.ReLU(inplace=True),
                                    se_ratio=0,skip=False)]) # no skips and attention

        self.layers = nn.Sequential(*modules)

    def forward(self, x):
        return self.layers(x)


class ResidualRepeatWav2Letter(nn.Module):
    def __init__(self,config):
        super(ResidualRepeatWav2Letter, self).__init__()

        size = config.size
        cnn_width = config.cnn_width
        bnorm = config.bnorm
        bnm = config.bnm
        dropout = config.dropout
        repeat_layers = config.repeat_layers
        kernel_size = config.kernel_size # wav2letter default - 7
        padding = kernel_size // 2
        se_ratio = config.se_ratio
        skip = config.skip
        decoder_girth = config.decoder_girth

        self.denoise = config.denoise if 'denoise' in config else False
        self.groups = config.groups if 'groups' in config else 1
        self.decoder_type = config.decoder_type if 'decoder_type' in config else 'pointwise'
        self.num_classes = config.num_classes if 'num_classes' in config else 0
        self.nonlinearity = config.nonlinearity if 'nonlinearity' in config else nn.ReLU(inplace=True)
        decoder_layers = config.decoder_layers if 'decoder_layers' in config else 2
        vary_cnn_width = config.vary_cnn_width if 'vary_cnn_width' in config else False

        if vary_cnn_width:
            # start with vary_cnn_width // 4
            # multiply by 2 after each downscaling layer
            # cnn_width = cnn_width // 4
            cnn_width = cnn_width // 2

        downsampled_blocks = []
        downsampled_subblocks = []
        if 'add_downsample' in config:
            assert config.add_downsample in [2, 4]
            # always at the beginning of each block
            downsampled_subblocks.append(0)
            if config.add_downsample == 2:
                # prolog has stride 2
                # add one more stride after one block
                downsampled_blocks.append(1)
            elif config.add_downsample == 4:
                # add one more stride after one more block
                downsampled_blocks.append(1)
                downsampled_blocks.append(2)

        separable = config.separable if 'separable' in config else False
        if separable:
            print('Using a separable CNN')
            Block = SeparableRepeatBlock
        else:
            Block = ResCNNRepeatBlock

        inverted_bottleneck = config.inverted_bottleneck if 'inverted_bottleneck' in config else False
        if inverted_bottleneck:
            print('Using inverted bottleneck')

        # "prolog"
        # no skips and attention
        kwargs = {
            '_in': 161, 'out': cnn_width, 'kernel_size': kernel_size,
            'padding': padding, 'stride': 2, 'bnm': bnm,'bias': not bnorm, 'dropout': dropout,
            'nonlinearity': self.nonlinearity,
            'se_ratio': 0, 'skip': False, 'repeat': 1,
            'bnorm': bnorm
        }
        if self.groups > 1: kwargs['groups'] = self.groups
        modules = [Block(**kwargs)]

        # main convs
        # 3 blocks
        dilated_blocks = config.dilated_blocks
        dilation_level = 2
        dilated_subblocks = [1,2]

        repeat_start = 2
        repeat_mid = 2
        repeat_end = 1

        repeats = [repeat_start,
                   repeat_mid,
                   repeat_end]

        # add layer down-scaling
        if inverted_bottleneck:
            kwargs = {
                '_in': cnn_width, 'out': cnn_width//4, 'kernel_size': kernel_size,
                'padding': padding, 'stride': 1, 'bnm': bnm,'bias': not bnorm, 'dropout': dropout,
                'nonlinearity': self.nonlinearity,
                'se_ratio': 0, 'skip': False, 'repeat': 1,
                'bnorm': bnorm
            }
            if self.groups > 1: kwargs['groups'] = self.groups
            modules.extend([Block(**kwargs)]) # no skips and attention

        for j in range(0, 3):
            for _ in range(0, repeat_layers//3):
                # 1221 dilation blocks
                dilation = 1 + (j in dilated_blocks) * (_ in dilated_subblocks) * (dilation_level - 1)
                stride = 1 + (j in downsampled_blocks) * (_ in downsampled_subblocks) * 1
                if stride == 2:
                    # add extra downsampling layer
                    print('Downsampling block {} / subblock {}'.format(j, _))
                    kwargs = {
                        '_in': cnn_width, 'out': cnn_width, 'kernel_size': kernel_size,
                        'padding': padding, 'dilation': dilation,
                        'stride': stride, 'bnm': bnm,'bias': not bnorm, 'dropout': dropout,
                        'nonlinearity': self.nonlinearity,
                        'se_ratio': 0, 'skip': False, 'repeat': 1,
                        'inverted_bottleneck': inverted_bottleneck,
                        'bnorm': bnorm
                    }
                    if self.groups > 1: kwargs['groups'] = self.groups
                    modules.extend(
                        [Block(**kwargs)]
                    )
                kwargs = {
                    '_in': cnn_width, 'out': cnn_width, 'kernel_size': kernel_size,
                    'padding': padding, 'dilation': dilation,
                    'stride': 1, 'bnm': bnm,'bias': not bnorm, 'dropout': dropout,
                    'nonlinearity': self.nonlinearity,
                    'se_ratio': se_ratio, 'skip': skip, 'repeat': repeats[j],
                    'inverted_bottleneck': inverted_bottleneck,
                    'bnorm': bnorm
                }
                if self.groups > 1: kwargs['groups'] = self.groups
                modules.extend(
                    [Block(**kwargs)]
                )
            if vary_cnn_width and j == 1:
                # transition layer
                trans_kwargs = {**kwargs,
                                'skip': False,
                                'repeat': 1,
                                'se_ratio': 0,
                                '_in': cnn_width,
                                'out': cnn_width * 2}
                modules.extend(
                    [Block(**trans_kwargs)]
                )
                cnn_width *= 2

        # add layer up-scaling
        if inverted_bottleneck:
            kwargs = {
                '_in': cnn_width//4, 'out': cnn_width, 'kernel_size': kernel_size,
                'padding': padding, 'stride': 1, 'bnm': bnm,'bias': not bnorm, 'dropout': dropout,
                'nonlinearity': nn.ReLU(inplace=True),
                'se_ratio': 0, 'skip': False, 'repeat': 1
            }
            if self.groups > 1: kwargs['groups'] = self.groups
            modules.extend([Block(**kwargs)]) # no skips and attention

        if self.decoder_type == 'pointwise':
            # "epilog"
            kwargs = {
                '_in': cnn_width, 'out': size, 'kernel_size': 31,
                'padding': 15, 'stride': 1, 'bnm': bnm,'bias': not bnorm, 'dropout': dropout,
                'nonlinearity': self.nonlinearity,
                'se_ratio': 0, 'skip': False, 'repeat': 1,
                'bnorm': bnorm
            }
            if self.groups > 1: kwargs['groups'] = self.groups
            modules.extend([Block(**kwargs)]) # no skips and attention

            kwargs = {
                '_in': size, 'out': size, 'kernel_size': 1,
                'padding': 0, 'stride': 1, 'bnm': bnm,'bias': not bnorm, 'dropout': dropout,
                'nonlinearity': self.nonlinearity,
                'se_ratio': 0, 'skip': False, 'repeat': 1,
                'bnorm': bnorm
            }
            if self.groups > 1: kwargs['groups'] = self.groups
            modules.extend([Block(**kwargs)]) # no skips and attention
        elif self.decoder_type == 'transformer':
            layer = nn.TransformerEncoderLayer(d_model=size,
                                               nhead=8,
                                               dim_feedforward=size * decoder_girth,
                                               dropout=dropout)
            self.decoder = nn.TransformerEncoder(layer, decoder_layers)
        elif self.decoder_type == 'plain_gru':
            # retain the large last kernel for now
            # make overall size smaller though
            kwargs = {
                '_in': cnn_width, 'out': size, 'kernel_size': 31,
                'padding': 15, 'stride': 1, 'bnm': bnm,'bias': not bnorm, 'dropout': dropout,
                'nonlinearity': self.nonlinearity,
                'se_ratio': 0, 'skip': False, 'repeat': 1,
                'bnorm': bnorm
            }
            if self.groups > 1: kwargs['groups'] = self.groups
            modules.extend([Block(**kwargs)])  # no skips and attention

            rnn_kwargs = {
                'input_size': size, 'hidden_size': size, 'rnn_type': nn.GRU,
                'bidirectional': True, 'batch_norm': True, 'batch_first': False
            }
            rnns = []
            for _ in range(decoder_layers):
                rnn = BatchRNN(**rnn_kwargs)
                rnns.append(rnn)
            self.decoder = nn.Sequential(*rnns)
        elif self.decoder_type == 'attention':
            # retain the large last kernel for now
            # make overall size smaller though
            kwargs = {
                '_in': cnn_width, 'out': size, 'kernel_size': 31,
                'padding': 15, 'stride': 1, 'bnm': bnm,'bias': not bnorm, 'dropout': dropout,
                'nonlinearity': self.nonlinearity,
                'se_ratio': 0, 'skip': False, 'repeat': 1,
                'bnorm': bnorm
            }
            if self.groups > 1: kwargs['groups'] = self.groups
            modules.extend([Block(**kwargs)])  # no skips and attention

            # size serves as hidden_size of all GRU models
            attention = BahdanauAttention(size, query_size=size)
            self.decoder = Decoder(256, size,
                                   self.num_classes, attention,
                                   num_layers=2, dropout=dropout,
                                   sos_index=self.num_classes-2)
        elif self.decoder_type == 'double_supervision':
            # in case of double supervision just use the longer
            # i.e. s2s = blank(pad) + base_num + space + eos + sos
            # ctc      = blank(pad) + base_num + space + 2
            # len(ctc) = len(s2s) - 1
            # s2s is the last one

            # retain the large last kernel for now
            # make overall size smaller though
            kwargs = {
                '_in': cnn_width, 'out': size, 'kernel_size': 31,
                'padding': 15, 'stride': 1, 'bnm': bnm,'bias': not bnorm, 'dropout': dropout,
                'nonlinearity': self.nonlinearity,
                'se_ratio': 0, 'skip': False, 'repeat': 1,
                'bnorm': bnorm
            }
            if self.groups > 1: kwargs['groups'] = self.groups
            modules.extend([Block(**kwargs)])  # no skips and attention

            # a CTC decoder module
            rnn_kwargs = {
                'input_size': size, 'hidden_size': size, 'rnn_type': nn.GRU,
                'bidirectional': True, 'batch_norm': True, 'batch_first': False
            }
            rnns = []
            for _ in range(2):
                rnn = BatchRNN(**rnn_kwargs)
                rnns.append(rnn)
            self.ctc_decoder = nn.Sequential(*rnns)
            self.ctc_fc = nn.Conv1d(in_channels=size,
                                    out_channels=self.num_classes - 1,  # exclude sos and eos but include 2
                                    kernel_size=1)

            # a slim S2S post-processing module
            # size serves as hidden_size of all GRU models
            attention = BahdanauAttention(size, query_size=size)
            self.s2s_decoder = Decoder(256, size,
                                       self.num_classes, attention,
                                       num_encoder_layers=2, # shorter encoder because of ctc decoder
                                       num_decoder_layers=2, dropout=dropout,
                                       sos_index=self.num_classes-2)
            # s2s already contains a generator "fc" module inside
        else:
            raise NotImplementedError('{} decoder not implemented'.format(self.decoder))

        if self.denoise:
            assert not inverted_bottleneck
            assert len(modules) == 1 + 3 * (repeat_layers // 3) + 2 + 1 + 1
            # do not forget about additional down-scale blocks
            self.encoder = nn.ModuleDict({
                'conv1':      nn.Sequential(*modules[                                 : 1 + 1 * (repeat_layers // 3) + 0]),
                'conv2':      nn.Sequential(*modules[1 + 1 * (repeat_layers // 3)     : 1 + 2 * (repeat_layers // 3) + 1]),
                'conv3':      nn.Sequential(*modules[1 + 2 * (repeat_layers // 3) + 1 : 1 + 3 * (repeat_layers // 3) + 2]),
                'final_conv': nn.Sequential(*modules[1 + 3 * (repeat_layers // 3) + 2 : ]),
            })
            self.denoise    = LinkNetDenoising(filters=[161]+[cnn_width]*3)
        else:
            self.layers = nn.Sequential(*modules)

    def forward(self, x,
                trg=None):
        if self.denoise:

            # incur some additional overhead here
            e1 = x
            e2 = self.encoder['conv1'](e1)
            e3 = self.encoder['conv2'](e2)
            e4 = self.encoder['conv3'](e3)

            # assert e1.size() == denoise_mask.size()
            # plain sigmoid gate
            # e1_denoised = e1 * torch.sigmoid(denoise_mask)

            denoise_mask = self.denoise(e1, e2, e3, e4)

            return (self.encoder['final_conv'](e4),
                    denoise_mask)
        else:
            if self.decoder_type == 'plain_gru':
                encoded = self.layers(x)
                # DS2 legacy code assumes T*N*H input
                # i.e.        length * batch    * channels
                # instead of  batch  * channels * length
                return self.decoder(
                    encoded.permute(2, 0, 1).contiguous()
                    ).permute(1, 2, 0).contiguous()
            elif self.decoder_type == 'transformer':
                encoded = self.layers(x)
                # https://pytorch.org/docs/stable/nn.html#transformer
                # src: (S, N, E)
                # instead of  batch  * channels * length
                return self.decoder(
                    encoded.permute(2, 0, 1).contiguous()
                    ).permute(1, 2, 0).contiguous()
            elif self.decoder_type == 'attention':
                # transform cnn format (batch, channel, length)
                # to rnn format (batch, length, channel)
                cnn_states = self.layers(x).permute(0, 2, 1).contiguous()
                return self.decoder(cnn_states,
                                    trg=trg)
            elif self.decoder_type == 'double_supervision':
                # DS2 legacy code assumes T*N*H input
                # i.e.        length * batch    * channels
                # instead of  batch  * channels * length like in CNNs
                cnn_states = self.layers(x)
                # print(trg.size())
                # print('cnn_states {}' .format(cnn_states.size()))
                ctc_states = self.ctc_decoder(
                    cnn_states.permute(2, 0, 1).contiguous()
                    )
                # print('ctc_states {}' .format(ctc_states.size()))
                ctc_out = self.ctc_fc(
                    ctc_states.permute(1, 2, 0).contiguous()
                    ).permute(0, 2, 1).contiguous()
                # print('ctc_out {}' .format(ctc_out.size()))
                s2s_out = self.s2s_decoder(ctc_states.permute(1, 0, 2).contiguous(),
                                           trg=trg)
                # print('s2s_out {}' .format(s2s_out.size()))
                return ctc_out, s2s_out
            elif self.decoder_type == 'pointwise':
                return self.layers(x)
            else:
                raise NotImplementedError('Forward function for {} decoder not implemented'.format(self.decoder))


class GLUBlock(nn.Module):
    def __init__(self,
                 _in=1,
                 out=400,
                 kernel_size=13,
                 stride=1,
                 padding=0,
                 dropout=0.2,
                 bnm=0.1
                 ):
        super(GLUBlock, self).__init__()

        self.conv = nn.Conv1d(_in,
                              out,
                              kernel_size,
                              stride=stride,
                              padding=padding)
        # self.conv = weight_norm(self.conv, dim=1)
        # self.norm = nn.InstanceNorm1d(out)
        self.norm = nn.BatchNorm1d(out//2,
                                   momentum=bnm)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        x = self.conv(x)
        x = glu(x,dim=1)
        x = self.norm(x)
        x = self.dropout(x)
        return x


class CNNBlock(nn.Module):
    def __init__(self,
                 _in=1,
                 out=400,
                 kernel_size=13,
                 stride=1,
                 padding=0,
                 dropout=0.1,
                 bnm=0.1,
                 nonlinearity=nn.ReLU(inplace=True),
                 bias=True
                 ):
        super(CNNBlock, self).__init__()

        self.conv = nn.Conv1d(_in,
                              out,
                              kernel_size,
                              stride=stride,
                              padding=padding,
                              bias=bias)
        self.norm = nn.BatchNorm1d(out,
                                   momentum=bnm)
        self.nonlinearity = nonlinearity
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        x = self.conv(x)
        x = self.norm(x)
        x = self.nonlinearity(x)
        x = self.dropout(x)
        return x


class ResCNNBlock(nn.Module):
    def __init__(self,
                 _in=1,
                 out=400,
                 kernel_size=13,
                 stride=1,
                 padding=0,
                 dropout=0.1,
                 bnm=0.1,
                 nonlinearity=nn.ReLU(inplace=True),
                 bias=True,
                 se_ratio=0,
                 skip=False,
                 inverted_bottleneck=False):
        super(ResCNNBlock, self).__init__()

        self.conv = nn.Conv1d(_in,
                              out,
                              kernel_size,
                              stride=stride,
                              padding=padding,
                              bias=bias)
        self.norm = nn.BatchNorm1d(out,
                                   momentum=bnm)
        self.nonlinearity = nonlinearity
        self.dropout = nn.Dropout(dropout)
        self.se_ratio = se_ratio
        self.skip = skip
        self.has_se = (self.se_ratio is not None) and (0 < self.se_ratio <= 1)
        # Squeeze and Excitation layer, if required
        if self.has_se:
            num_squeezed_channels = max(1, int(_in * self.se_ratio))
            self._se_reduce = Conv1dSamePadding(in_channels=out, out_channels=num_squeezed_channels, kernel_size=1)
            self._se_expand = Conv1dSamePadding(in_channels=num_squeezed_channels, out_channels=out, kernel_size=1)

    def forward(self, x):
        # be a bit more memory efficient during ablations
        if self.skip:
            inputs = x
        x = self.conv(x)
        x = self.norm(x)
        x = self.nonlinearity(x)
        x = self.dropout(x)
        if self.has_se:
            x_squeezed = F.adaptive_avg_pool1d(x, 1) # channel dimension
            x_squeezed = self._se_expand(relu_fn(self._se_reduce(x_squeezed)))
            x = torch.sigmoid(x_squeezed) * x
        if self.skip:
            x = x + inputs
        return x


class ResCNNRepeatBlock(nn.Module):
    def __init__(self,
                 _in=1,
                 out=400,
                 kernel_size=13,
                 stride=1,
                 padding=0,
                 dilation=1,
                 dropout=0.1,
                 bnm=0.1,
                 nonlinearity=nn.ReLU(inplace=True),
                 bias=True,
                 se_ratio=0,
                 skip=False,
                 repeat=1,
                 inverted_bottleneck=False):
        super(ResCNNRepeatBlock, self).__init__()

        self.skip = skip
        has_se = (se_ratio is not None) and (0 < se_ratio <= 1)
        dropout = nn.Dropout(dropout)
        if has_se:
            # squeeze after each block
            scse = SCSE(out,
                        kernel_size=1, se_ratio=se_ratio)

        modules = []
        if dilation>1:
            padding = dilation*(kernel_size-1)//2

        # just stick all the modules together
        for i in range(0,repeat):
            if i==0:
                modules.extend([nn.Conv1d(_in, out, kernel_size,
                                          stride=stride,
                                          padding=padding,
                                          dilation=dilation,
                                          bias=bias),
                                nn.BatchNorm1d(out,
                                               momentum=bnm),
                                nonlinearity,
                                dropout])
            else:
                modules.extend([nn.Conv1d(out, out, kernel_size,
                                          stride=stride,
                                          padding=padding,
                                          dilation=dilation,
                                          bias=bias),
                                nn.BatchNorm1d(out,
                                               momentum=bnm),
                                nonlinearity,
                                dropout])
            if has_se:
                modules.extend([scse])

        self.layers = nn.Sequential(*modules)

    def forward(self, x):
        if self.skip:  # be a bit more memory efficient during ablations
            inputs = x
        x = self.layers(x)
        if self.skip:
            x = x + inputs
        return x


class SeparableRepeatBlock(nn.Module):
    def __init__(self,
                 _in=1,
                 out=400,
                 kernel_size=13,
                 stride=1,
                 padding=0,
                 dilation=1,
                 dropout=0.1,
                 bnm=0.1,
                 nonlinearity=nn.ReLU(inplace=True),
                 bias=False,
                 bnorm=True,
                 se_ratio=0,
                 skip=False,
                 repeat=1,
                 mix_channels=True,
                 inverted_bottleneck=False,
                 groups=12):
        super(SeparableRepeatBlock, self).__init__()

        inverted_bottleneck_scale = 4
        groups = groups
        self.skip = skip
        has_se = (se_ratio is not None) and (0 < se_ratio <= 1)
        dropout = nn.Dropout(dropout)

        if bnorm:
            if True:
                norm_block = nn.BatchNorm1d
            else:
                norm_block = SwitchNorm1d
        else:
            norm_block = nn.Identity

        last_out = out
        if inverted_bottleneck:
            _in = _in // inverted_bottleneck_scale
            last_out = out // inverted_bottleneck_scale

        modules = []
        if dilation>1:
            padding = dilation*(kernel_size-1)//2

        # just stick all the modules together
        for i in range(0, repeat):
            if repeat == 1:
                in_ch = _in
                out_ch = last_out
            else:
                if i==0:
                    # first conv
                    in_ch = _in
                    out_ch = out
                elif i==(repeat - 1):
                    # last conv
                    in_ch = out
                    out_ch = last_out
                else:
                    # mid conv
                    in_ch = out
                    out_ch = out

            modules.extend([nn.Conv1d(in_ch, out_ch, kernel_size,
                                      stride=stride,
                                      padding=padding,
                                      dilation=dilation,
                                      groups=groups if (in_ch % groups + out_ch % groups) == 0 else 1,
                                      bias=bias),
                            norm_block(out_ch,
                                       momentum=bnm),
                            nonlinearity,
                            dropout])

            if has_se:
                # apply lightweight attention layer
                modules.extend([SCSE(out_ch,
                                    kernel_size=1,
                                    se_ratio=se_ratio)])
            if mix_channels:
                # mix the separated channels
                modules.extend([Conv1dSamePadding(in_channels=out_ch,
                                                  out_channels=out_ch,
                                                  kernel_size=1),
                                nn.BatchNorm1d(out_ch,
                                               momentum=bnm),
                                nonlinearity,
                                dropout])
        self.layers = nn.Sequential(*modules)

    def forward(self, x):
        if self.skip:  # be a bit more memory efficient during ablations
            inputs = x
        x = self.layers(x)
        if self.skip:
            x = x + inputs
        return x


 # SCSE attention block https://arxiv.org/abs/1803.02579
class SCSE(nn.Module):
    def __init__(self,
                 in_channels,
                 kernel_size=1, se_ratio=0.1):
        super(SCSE, self).__init__()
        num_squeezed_channels = max(1, int(in_channels * se_ratio))
        self._se_reduce = Conv1dSamePadding(in_channels=in_channels, out_channels=num_squeezed_channels, kernel_size=1)
        self._se_expand = Conv1dSamePadding(in_channels=num_squeezed_channels, out_channels=in_channels, kernel_size=1)

    def forward(self, x):
        x_squeezed = F.adaptive_avg_pool1d(x, 1) # channel dimension
        x_squeezed = self._se_expand(relu_fn(self._se_reduce(x_squeezed)))
        x = torch.sigmoid(x_squeezed) * x
        return x

class TDS(nn.Module):
    def __init__(self, config):
        super(TDS, self).__init__()

        self.input_channels = config.input_channels
        h = config.h # stft or mel
        self.h = h
        dropout = config.dropout
        kernel_size = config.kernel_size

        blocks =  config.blocks
        strides = config.strides
        self.strides = strides
        repeats =  config.repeats
        channels = config.channels
        output_size = config.output_size

        self.channels = channels

        assert len(repeats) == blocks
        assert len(channels) == blocks
        assert len(strides) == blocks
        # https://github.com/facebookresearch/wav2letter/blob/master/recipes/librispeech/configs/seq2seq_tds/network.arch#L1

        """
        REMOVE LATER
        V -1 NFEAT 1 0
        C2 1 10 21 1 2 1 -1 -1
        R
        DO 0.2
        LN 3
        TDS 10 21 80 0.2
        TDS 10 21 80 0.2
        C2 10 14 21 1 2 1 -1 -1
        R
        DO 0.2
        LN 3
        TDS 14 21 80 0.2
        TDS 14 21 80 0.2
        TDS 14 21 80 0.2
        C2 14 18 21 1 2 1 -1 -1
        R
        DO 0.2
        LN 3
        TDS 18 21 80 0.2
        TDS 18 21 80 0.2
        TDS 18 21 80 0.2
        TDS 18 21 80 0.2
        TDS 18 21 80 0.2
        TDS 18 21 80 0.2
        V 0 1440 1 0
        RO 1 0 3 2
        L 1440 1024
        """

        channels = [1] + channels
        modules = []
        for i, (repeat, stride) in enumerate(zip(repeats,
                                                 strides)):
            modules.extend([
                Conv2dSamePadding(channels[i],channels[i+1],(kernel_size,1),
                                  stride=(stride, 2 if i==0 else 1)), # adhere to 80 channels in mel, 161 => 80
                nn.ReLU(inplace=True),
                nn.Dropout(dropout),
                SeqLayerNormView(), # move the normalized channel to the last channel
                nn.LayerNorm([channels[i+1], h]),
                SeqLayerNormRestore() # revert
            ])
            modules.extend([TDSBlock(channels[i+1],
                                     kernel_size,
                                     h,
                                     dropout) for j in range(0,repeat)])

        self.linear = nn.Conv1d(in_channels=self.h * self.channels[-1],
                                out_channels=output_size, kernel_size=1)
        self.layers = nn.Sequential(*modules)

    def forward(self, x):
        if DEBUG: print('Input {}'.format(x.size()))
        batch_size = x.size(0)
        time = x.size(2)
        # mel input is 161
        # the inside of the model is 80
        assert x.size(1) == self.input_channels
        # V -1 NFEAT 1 0 (w2l++)
        # (batch, time, h, 1) (pytorch)
        # or should it be (batch, 1, time, h) (pytorch)
        x = x.view(batch_size, 1, time, self.input_channels)
        if DEBUG: print('First view {}'.format(x.size()))
        x = self.layers(x)
        if DEBUG: print('After layers {}'.format(x.size()))
        time_downsampled = x.size(2)
        time_ratio = (time + 10) // time_downsampled
        if DEBUG: print('Effective network downsampling is {}'.format(time_ratio))
        # if time_ratio not in [2, 4, 8]:
        #    print(time, time_downsampled, time_ratio)
        # assert time_ratio in [2, 4, 8]
        # V 0 1440 1 0
        # RO 1 0 3 2
        x = x.view(batch_size, self.h * self.channels[-1], time_downsampled)
        x = self.linear(x)
        if DEBUG: print('After first fc {}'.format(x.size()))
        return x

# http://arxiv.org/abs/1904.02619
class TDSBlock(nn.Module):
    def __init__(self,
                 channels,
                 kernel_width,
                 h,
                 dropout
                ):
        super(TDSBlock, self).__init__()

        # https://github.com/facebookresearch/wav2letter/blob/153d6665ab008835560854d5071c106400c1cc21/src/module/TDSBlock.cpp#L26-L29
        # here they have l and l2
        # though in all of the places l2 equals l
        self.h = h
        self.c = channels
        l = self.c * self.h

        self.conv = nn.Sequential(
            Conv2dSamePadding(channels,channels,(kernel_width,1),
                              stride=1),
            nn.ReLU(inplace=True),
            nn.Dropout(dropout)
        )
        self.fc = nn.Sequential(
            nn.Linear(l, l),
            nn.ReLU(inplace=True),
            nn.Dropout(dropout),
            nn.Linear(l, l),
            # this differs a bit from this
            # https://github.com/facebookresearch/wav2letter/blob/153d6665ab008835560854d5071c106400c1cc21/src/module/TDSBlock.cpp#L41
            # here dropout is applied after reorder + view
            nn.Dropout(dropout),
        )
        # careful here
        # https://pytorch.org/docs/stable/nn.html#layernorm
        # https://github.com/facebookresearch/wav2letter/blob/153d6665ab008835560854d5071c106400c1cc21/src/module/TDSBlock.cpp#L46
        # fc.add(View(af::dim4(-1, h, c, 0)));
        # If a single integer is used, it is treated as a singleton list
        # and this module will normalize over the last dimension
        # which is expected to be of that specific size.
        self.ln_conv = nn.LayerNorm([self.c, self.h])
        self.ln_fc = nn.LayerNorm([self.c, self.h])

    def forward(self, x):
        # I guess input should look like
        # fc.add(View(af::dim4(-1, h, c, 0)))
        # or (time, h, c, batch) # https://github.com/facebookresearch/wav2letter/blob/master/docs/arch.md#writing-architecture-files
        # https://github.com/facebookresearch/wav2letter/blob/153d6665ab008835560854d5071c106400c1cc21/src/module/TDSBlock.cpp#L40
        # in PyTorch it would be
        # (batch, time, h, c) or should it be (batch, c, time, h) ?
        out = x
        out = self.conv(out) + out
        # Given normalized_shape=[10], expected input with shape [*, 10], but got input of size[2, 10, 120, 81]
        out = self.ln_conv(out.permute(0, 2, 1, 3)).permute(0, 2, 1, 3).contiguous()

        # fc.add(View(af::dim4(-1, l, 1, 0)))
        # fc.add(Reorder(1, 0, 2, 3))
        # (time, h, c, batch) => (time, h*с, 1, batch) => (h*с, time, 1, batch)
        # in Pytorch terms
        # (batch, time, h, c) => (batch, time, h*с, 1) => (batch, h*с, time, 1)
        # or should it be
        # (batch, c, time, h) => (batch, time, h*с, 1) => (batch, h*с, time, 1)
        # or
        # (batch, c, time, h) => (batch, h*с, time, 1) => (batch, time, h*с, 1) ?
        out = out.view(out.size(0), # batch
                       out.size(2), # time
                       self.h * self.c, # h * c
                       ) # .permute(0, 2, 1, 3) do we need this permute?
        # if DEBUG: print(out.size())
        # if DEBUG: print(self.fc(out).size())
        out = self.fc(out) + out
        # fc.add(Reorder(1, 0, 2, 3));
        # fc.add(View(af::dim4(-1, h, c, 0)));
        # (batch, time, h, c) or (batch, c, time, h) ?
        # .permute(0, 2, 1, 3)
        out = out.view(out.size(0), # batch
                       self.c, # c
                       out.size(1), # time
                       self.h, # h
                       )
        # Given normalized_shape=[10], expected input with shape [*, 10], but got input of size[2, 10, 120, 81]
        out = self.ln_fc(out.permute(0, 2, 1, 3)).permute(0, 2, 1, 3).contiguous()
        return out


# http://arxiv.org/abs/1904.03288
class Jasper_conv_block(nn.Module):
    def __init__(self,
                 repeat,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride=1,
                 dilation=1,
                 dropout=0,
                 residual=True,
                 bn_momentum=0.1,
                 bn_eps=1e-05,
                 activation_fn=None):
        super(Jasper_conv_block, self).__init__()

        self.bn =nn.ModuleList([nn.BatchNorm1d(num_features=out_channels, eps=bn_eps, momentum=bn_momentum) for i in range(repeat)])

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.residual = residual

        module_list = []
        if stride==1:
            module_list.append(JasperConv1dSame(self.in_channels, self.out_channels, kernel_size, stride, dilation))
        else:
            module_list.append(nn.Conv1d(self.in_channels, self.out_channels,kernel_size,
                                         stride=stride, dilation=dilation, padding=kernel_size//2))
        for rep in range(repeat-1):
            module_list.append(JasperConv1dSame(self.out_channels, self.out_channels, kernel_size, stride, dilation))

        self.module_list = nn.ModuleList(module_list)

        self.activation_fn = activation_fn()
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, x, res_input=None):
        for i, module in enumerate(self.module_list):
            x = module(x)
            x = self.bn[i](x)
            if (i == (len(self.module_list)-1)) & (res_input is not None):
                x = x + res_input
            x = self.activation_fn(x)
            x = self.dropout(x)
        return x


class JasperConv1dSame(nn.Conv1d):
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride=1,
                 dilation=1,
                 groups=1,
                 bias=True):
        super().__init__(in_channels, out_channels, kernel_size, stride, 0, dilation, groups, bias)

    def forward(self, x):
        out_len = x.shape[2]
        padding = math.ceil(((out_len - 1) * self.stride[0] + self.kernel_size[0] + \
                             (self.kernel_size[0] - 1) * (self.dilation[0] - 1) - out_len))
        if padding > 0:
            x = F.pad(x, (padding//2, padding-padding//2))
        return F.conv1d(x, self.weight, self.bias, self.stride, self.padding, self.dilation, self.groups)


class JasperNet(nn.Module):
    def __init__(self, config=None):
        super(JasperNet, self).__init__()

        self.dense_residual = config['dense_residual']
        block_list = []
        all_skip_convs = []
        all_skip_bns = []
        for i, repeat in enumerate(config['repeats']):

            in_channels = config['input_channels'] if i==0 else config['channels'][i-1]
            out_channels = config['channels'][i]
            kernel_size = config['kernel_sizes'][i]
            stride = config['strides'][i]
            dilation = config['dilations'][i]
            bn_momentum = config['bn_momentum']
            bn_eps = config['bn_eps']
            dropout = config.get('dropout', config['dropouts'][i])
            residual = bool(config['residual'][i])
            activation_fn = config['activation_fn']

            block_list.append(Jasper_conv_block(repeat=repeat, in_channels=in_channels, out_channels=out_channels,
                                         kernel_size=kernel_size, stride=stride, dilation=dilation,
                                         dropout=dropout, residual=residual,
                                         bn_momentum=bn_momentum, bn_eps=bn_eps, activation_fn=activation_fn))

            skip_convs = []
            skip_bns = []
            if residual:
                if self.dense_residual:
                    skip_convs = [nn.Conv1d(i.in_channels, out_channels, 1, bias=False) for i in block_list if i.residual]
                    skip_bns = [nn.BatchNorm1d(out_channels, bn_eps, bn_momentum) for i in block_list if i.residual]
                else:
                    skip_convs.append(nn.Conv1d(in_channels, out_channels, 1, bias=False))
                    skip_bns.append(nn.BatchNorm1d(out_channels, bn_eps, bn_momentum))

            skip_convs = nn.ModuleList(skip_convs)
            skip_bns = nn.ModuleList(skip_bns)

            all_skip_convs.append(skip_convs)
            all_skip_bns.append(skip_bns)

        self.block_list = nn.ModuleList(block_list)
        self.all_skip_convs = nn.ModuleList(all_skip_convs)
        self.all_skip_bns = nn.ModuleList(all_skip_bns)
    def forward(self, input_, return_skips=False):
        residuals = []
        if return_skips:
            skips = []
        x = input_
        for i, block in enumerate(self.block_list):
            res = 0
            if block.residual:
                if self.dense_residual:
                    residuals.append(x)
                else:
                    residuals = [x]
                # assert len(self.all_skip_convs[i]) == len(residuals)
                for skip_conv, skip_bn, residual in zip(self.all_skip_convs[i],
                                                        self.all_skip_bns[i],
                                                        residuals):
                    res += skip_bn(skip_conv(residual))
                x = block(x, res)
                if return_skips:
                    skips.append(x)
            else:
                x = block(x)
        if return_skips:
            return x, skips
        return x


class Jasper_non_repeat(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 dilation=1, dropout=0, bn_momentum=0.1, bn_eps=1e-05, activation_fn=None):
        super(Jasper_non_repeat, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.bn = nn.BatchNorm1d(num_features=out_channels, eps=bn_eps, momentum=bn_momentum)
        if stride==1:
            self.conv = JasperConv1dSame(self.in_channels, self.out_channels, kernel_size, stride, dilation)
        else:
            self.conv = nn.Conv1d(self.in_channels, self.out_channels,kernel_size,
                                         stride=stride, dilation=dilation, padding=kernel_size//2)
        self.activation_fn = activation_fn()
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.activation_fn(x)
        x = self.dropout(x)
        return x


class Jasper_repeat(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size,
                 repeat_5=False, stride=1, dilation=1, dropout=0, bn_momentum=0.1, bn_eps=1e-05, activation_fn=None):
        super(Jasper_repeat, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.repeat_5 = repeat_5

        # main repeats inside block
        repeat_0 = JasperConv1dSame(self.in_channels, self.out_channels, kernel_size, stride, dilation)
        repeat_1 = JasperConv1dSame(self.out_channels, self.out_channels, kernel_size, stride, dilation)
        repeat_2 = JasperConv1dSame(self.out_channels, self.out_channels, kernel_size, stride, dilation)
        if repeat_5:
            repeat_3 = JasperConv1dSame(self.out_channels, self.out_channels, kernel_size, stride, dilation)
            repeat_4 = JasperConv1dSame(self.out_channels, self.out_channels, kernel_size, stride, dilation)

        # bns
        bn_0 = nn.BatchNorm1d(num_features=out_channels, eps=bn_eps, momentum=bn_momentum)
        bn_1 = nn.BatchNorm1d(num_features=out_channels, eps=bn_eps, momentum=bn_momentum)
        bn_2 = nn.BatchNorm1d(num_features=out_channels, eps=bn_eps, momentum=bn_momentum)
        if repeat_5:
            bn_3 = nn.BatchNorm1d(num_features=out_channels, eps=bn_eps, momentum=bn_momentum)
            bn_4 = nn.BatchNorm1d(num_features=out_channels, eps=bn_eps, momentum=bn_momentum)

        self.residual = nn.Conv1d(in_channels, out_channels, 1, bias=False)
        self.res_bn = nn.BatchNorm1d(out_channels, bn_eps, bn_momentum)

        self.activation_fn = activation_fn()
        self.dropout = nn.Dropout(p=dropout)

        self.rep_0 = nn.Sequential(repeat_0, bn_0, self.activation_fn, self.dropout)
        self.rep_1 = nn.Sequential(repeat_1, bn_1, self.activation_fn, self.dropout)
        self.rep_2 = nn.Sequential(repeat_2, bn_2, self.activation_fn, self.dropout)
        if self.repeat_5:
            self.rep_3 = nn.Sequential(repeat_3, bn_3, self.activation_fn, self.dropout)
            self.rep_4 = nn.Sequential(repeat_4, bn_4, self.activation_fn, self.dropout)

    def forward(self, input_, return_skips=False):
        x = self.rep_0(input_)
        x = self.rep_1(x)
        x = self.rep_2(x)
        if self.repeat_5:
            x = self.rep_3(x)
            x = self.rep_4(x)
        res = self.res_bn(self.residual(input_))
        x = x + res
        x = self.activation_fn(x)
        x = self.dropout(x)
        if return_skips:
            return x, res
        return x, None


class JasperNetEasy(nn.Module):
    def __init__(self, config=None):
        super(JasperNetEasy,self).__init__()
        self.in_channels = config['input_channels']
        self.bn_momentum = config['bn_momentum']
        self.bn_eps = config['bn_eps']
        self.activation_fn = config['activation_fn']
        self.repeats = config['repeats']
        self.num_modules = config['num_modules']

        assert self.num_modules in [5,10]

        assert self.repeats in [3,5]
        self.repeat_5 = False
        if self.repeats == 5:
            self.repeat_5 = True

        self.init = Jasper_non_repeat(self.in_channels, out_channels=256, kernel_size=11, stride=2, dilation=1,
                                      dropout=0.2, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps, activation_fn=self.activation_fn)

        self.block_0 = Jasper_repeat(in_channels=256, out_channels=256, kernel_size=11, repeat_5=self.repeat_5, stride=1, dilation=1,
                                     dropout=0.2, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps, activation_fn=self.activation_fn)
        self.block_1 = Jasper_repeat(in_channels=256, out_channels=384, kernel_size=13, repeat_5=self.repeat_5, stride=1, dilation=1,
                                     dropout=0.2, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps, activation_fn=self.activation_fn)
        self.block_2 = Jasper_repeat(in_channels=384, out_channels=512, kernel_size=17, repeat_5=self.repeat_5, stride=1, dilation=1,
                                     dropout=0.2, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps, activation_fn=self.activation_fn)
        self.block_3 = Jasper_repeat(in_channels=512, out_channels=640, kernel_size=21, repeat_5=self.repeat_5, stride=1, dilation=1,
                                     dropout=0.3, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps, activation_fn=self.activation_fn)
        self.block_4 = Jasper_repeat(in_channels=640, out_channels=768, kernel_size=25, repeat_5=self.repeat_5, stride=1, dilation=1,
                                     dropout=0.3, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps, activation_fn=self.activation_fn)

        if self.num_modules == 10:
            self.block_0_0= Jasper_repeat(in_channels=256, out_channels=256, kernel_size=11, repeat_5=self.repeat_5, stride=1,
                                          dilation=1, dropout=0.2, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps,
                                          activation_fn=self.activation_fn)
            self.block_1_0 = Jasper_repeat(in_channels=384, out_channels=384, kernel_size=13, repeat_5=self.repeat_5, stride=1,
                                           dilation=1, dropout=0.2, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps,
                                           activation_fn=self.activation_fn)
            self.block_2_0 = Jasper_repeat(in_channels=512, out_channels=512, kernel_size=17, repeat_5=self.repeat_5, stride=1,
                                           dilation=1, dropout=0.2, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps,
                                           activation_fn=self.activation_fn)
            self.block_3_0 = Jasper_repeat(in_channels=640, out_channels=640, kernel_size=21, repeat_5=self.repeat_5, stride=1,
                                           dilation=1, dropout=0.3, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps,
                                           activation_fn=self.activation_fn)
            self.block_4_0 = Jasper_repeat(in_channels=768, out_channels=768, kernel_size=25, repeat_5=self.repeat_5, stride=1, dilation=1,
                                           dropout=0.3, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps, activation_fn=self.activation_fn)

            self.block_0 = nn.Sequential(self.block_0, self.block_0_0)
            self.block_1 = nn.Sequential(self.block_1, self.block_1_0)
            self.block_2 = nn.Sequential(self.block_2, self.block_2_0)
            self.block_3 = nn.Sequential(self.block_3, self.block_3_0)
            self.block_4 = nn.Sequential(self.block_4, self.block_4_0)

        self.out_0 = Jasper_non_repeat(768, out_channels=896, kernel_size=29, stride=1, dilation=2,
                                       dropout=0.4, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps, activation_fn=self.activation_fn)
        self.out_1 = Jasper_non_repeat(896, out_channels=1024, kernel_size=1, stride=1, dilation=1,
                                       dropout=0.4, bn_momentum=self.bn_momentum, bn_eps=self.bn_eps, activation_fn=self.activation_fn)

    def forward(self, x, return_skips=False):
        if return_skips:
            skips = []
        x = self.init(x)
        x, skip1 = self.block_0(x, return_skips)
        assert skip1 == None
        x, skip2 = self.block_1(x, return_skips)
        x, skip3 = self.block_2(x, return_skips)
        x, skip4 = self.block_3(x, return_skips)
        x, skip5 = self.block_4(x, return_skips)
        x = self.out_0(x)
        x = self.out_1(x)
        return x


class SmallGLU(nn.Module):
    def __init__(self,config):
        super(SmallGLU, self).__init__()
        bnm = config.bnm
        dropout = config.dropout
        layer_outputs = [100,100,100,125,125,150,175,200,
                         225,250,250,250,300,300,375]
        layer_list = [
            GLUBlock(config.input_channels,200,13,1,6,dropout, bnm), # 1
            GLUBlock(100,200,3,1,(1),dropout, bnm), # 2
            GLUBlock(100,200,4,1,(2),dropout, bnm), # 3
            GLUBlock(100,250,5,1,(2),dropout, bnm), # 4
            GLUBlock(125,250,6,1,(3),dropout, bnm), # 5
            GLUBlock(125,300,7,1,(3),dropout, bnm), # 6
            GLUBlock(150,350,8,1,(4),dropout, bnm), # 7
            GLUBlock(175,400,9,1,(4),dropout, bnm), # 8
            GLUBlock(200,450,10,1,(5),dropout, bnm), # 9
            GLUBlock(225,500,11,1,(5),dropout, bnm), # 10
            GLUBlock(250,500,12,1,(6),dropout, bnm), # 11
            GLUBlock(250,500,13,1,(6),dropout, bnm), # 12
            GLUBlock(250,600,14,1,(7),dropout, bnm), # 13
            GLUBlock(300,600,15,1,(7),dropout, bnm), # 14
            GLUBlock(300,750,21,1,(10),dropout, bnm), # 15
        ]
        self.layers = nn.Sequential(*layer_list[:config.layer_num])
        self.last_channels = layer_outputs[config.layer_num-1]

    def forward(self, x):
        return self.layers(x)


class LargeGLU(nn.Module):
    def __init__(self,config):
        super(LargeGLU, self).__init__()
        layer_outputs = [200,220,242,266,292,321,353,388,426,
                         468,514,565,621,683,751,826,908]
        # in out kw stride padding dropout
        self.layers = nn.Sequential(
            # whole padding in one place
            GLUBlock(config.input_channels,400,13,1,170,0.2), # 1
            GLUBlock(200,440,14,1,0,0.214), # 2
            GLUBlock(220,484,15,1,0,0.228), # 3
            GLUBlock(242,532,16,1,0,0.245), # 4
            GLUBlock(266,584,17,1,0,0.262), # 5
            GLUBlock(292,642,18,1,0,0.280), # 6
            GLUBlock(321,706,19,1,0,0.300), # 7
            GLUBlock(353,776,20,1,0,0.321), # 8
            GLUBlock(388,852,21,1,0,0.347), # 9
            GLUBlock(426,936,22,1,0,0.368), # 10
            GLUBlock(468,1028,23,1,0,0.393), # 11
            GLUBlock(514,1130,24,1,0,0.421), # 12
            GLUBlock(565,1242,25,1,0,0.450), # 13
            GLUBlock(621,1366,26,1,0,0.482), # 14
            GLUBlock(683,1502,27,1,0,0.516), # 15
            GLUBlock(751,1652,28,1,0,0.552), # 16
            GLUBlock(826,1816,29,1,0,0.590), # 17
        )
        self.last_channels = layer_outputs[config.layer_num-1]

    def forward(self, x):
        return self.layers(x)


class LargeCNN(nn.Module):
    def __init__(self,config):
        super(LargeCNN, self).__init__()
        bnm = config.bnm
        dropout = config.dropout
        # in out kw stride padding dropout
        self.layers = nn.Sequential(
            # whole padding in one place
            CNNBlock(config.input_channels,200,13,2,6,dropout, bnm), # 1
            CNNBlock(200,220,14,1,7, dropout, bnm), # 2
            CNNBlock(220,242,15,1,7, dropout, bnm), # 3
            CNNBlock(242,266,16,1,8, dropout, bnm), # 4
            CNNBlock(266,292,17,1,8, dropout, bnm), # 5
            CNNBlock(292,321,18,1,9, dropout, bnm), # 6
            CNNBlock(321,353,19,1,9, dropout, bnm), # 7
            CNNBlock(353,388,20,1,10, dropout, bnm), # 8
            CNNBlock(388,426,21,1,10, dropout, bnm), # 9
            CNNBlock(426,468,22,1,11, dropout, bnm), # 10
            CNNBlock(468,514,23,1,11, dropout, bnm), # 11
            CNNBlock(514,565,24,1,12, dropout, bnm), # 12
            CNNBlock(565,621,25,1,12, dropout, bnm), # 13
            CNNBlock(621,683,26,1,13, dropout, bnm), # 14
            CNNBlock(683,751,27,1,13, dropout, bnm), # 15
            CNNBlock(751,826,28,1,14, dropout, bnm), # 16
            CNNBlock(826,826,29,1,14, dropout, bnm), # 17
        )
        self.last_channels = 826

    def forward(self, x):
        return self.layers(x)


class DotDict(dict):
    """
    a dictionary that supports dot notation
    as well as dictionary access notation
    usage: d = DotDict() or d = DotDict({'val1':'first'})
    set attributes: d.val2 = 'second' or d['val2'] = 'second'
    get attributes: d.val2 or d['val2']
    """
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__

    def __init__(self, dct):
        for key, value in dct.items():
            if hasattr(value, 'keys'):
                value = DotDict(value)
            self[key] = value


# wrap in module to use in sequential
class GLUModule(nn.Module):
    def __init__(self, dim=1):
        super(GLUModule, self).__init__()
        self.dim = 1

    def forward(self, x):
        return glu(x,dim=self.dim)


def relu_fn(x):
    """ Swish activation function """
    return x * torch.sigmoid(x)


class Conv1dSamePadding(nn.Conv1d):
    """ 2D Convolutions like TensorFlow """
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, dilation=1, groups=1, bias=True):
        super().__init__(in_channels, out_channels, kernel_size, stride, 0, dilation, groups, bias)
        self.stride = self.stride[0] # just a scalar

    def forward(self, x):
        iw = int(x.size()[-1])
        kw = int(self.weight.size()[-1])
        sw = self.stride
        ow = math.ceil(iw / sw)
        pad_w = max((ow - 1) * self.stride + (kw - 1) * self.dilation[0] + 1 - iw, 0)
        if pad_w > 0:
            x = F.pad(x, [pad_w//2, pad_w - pad_w//2])
        return F.conv1d(x, self.weight, self.bias, self.stride, self.padding, self.dilation, self.groups)


class Conv2dSamePadding(nn.Conv2d):
    """ 2D Convolutions like TensorFlow """
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, dilation=1, groups=1, bias=True):
        super().__init__(in_channels, out_channels, kernel_size, stride, 0, dilation, groups, bias)
        self.stride = self.stride if len(self.stride) == 2 else [self.stride[0]]*2

    def forward(self, x):
        ih, iw = x.size()[-2:]
        kh, kw = self.weight.size()[-2:]
        sh, sw = self.stride
        oh, ow = math.ceil(ih / sh), math.ceil(iw / sw)
        pad_h = max((oh - 1) * self.stride[0] + (kh - 1) * self.dilation[0] + 1 - ih, 0)
        pad_w = max((ow - 1) * self.stride[1] + (kw - 1) * self.dilation[1] + 1 - iw, 0)
        if DEBUG: print('Padding {}'.format(pad_h, pad_w))
        if pad_h > 0 or pad_w > 0:
            x = F.pad(x, [pad_w//2, pad_w - pad_w//2, pad_h//2, pad_h - pad_h//2])
        return F.conv2d(x, self.weight, self.bias, self.stride, self.padding, self.dilation, self.groups)


class SequentialView(nn.Module):
	def __init__(self):
		super(SequentialView, self).__init__()
	def forward(self, x):
		return x.permute(0, 2, 1).contiguous()

# a hack to use LayerNorm in sequential
# move the normalized dimension to the last dimension
class SeqLayerNormView(nn.Module):
	def __init__(self):
		super(SeqLayerNormView, self).__init__()
	def forward(self, x):
		return x.permute(0, 2, 1, 3)


# restore the original order
class SeqLayerNormRestore(nn.Module):
	def __init__(self):
		super(SeqLayerNormRestore, self).__init__()
	def forward(self, x):
		return x.permute(0, 2, 1, 3).contiguous()


class GaussianDropout(nn.Module):
    def __init__(self, alpha=1.0):
        super(GaussianDropout, self).__init__()
        self.alpha = torch.Tensor([alpha])

    def forward(self, x):
        """
        Sample noise   e ~ N(1, alpha)
        Multiply noise h = h_ * e
        """
        if self.train():
            # N(1, alpha)
            epsilon = torch.randn(x.size()) * self.alpha + 1
            epsilon = epsilon.to(self.device)
            return x * epsilon
        else:
            return x


class MultiSampleFC(nn.Module):
    def __init__(self,
                 heads=8,
                 dropout_rate=0.2,
                 in_channels=None,
                 out_channels=None):
        super(MultiSampleFC, self).__init__()
        self.heads = heads
        self.dropout_rate = dropout_rate
        self.layers = nn.Conv1d(in_channels=in_channels,
                                out_channels=out_channels,
                                kernel_size=1)

    def forward(self, x):
        """
        Idea similar to https://arxiv.org/abs/1905.09788
        Without multiple loss computation
        """
        if self.train():
            outputs = []
            prob = 1 - self.dropout_rate
            for i in range(self.heads):
                mask = torch.bernoulli(torch.full_like(x, prob))
                outputs.append(self.layers(x * mask))
            out = torch.mean(torch.stack(outputs, dim=0), dim=0)
            return out
        else:
            return self.layers(x)


class LinkNetDenoising(nn.Module):
    def __init__(self,
                 filters=[161, 768, 768, 768], # am states
                 nonlinearity=nn.ReLU
                ):
        super().__init__()

        self.decoder3 = DecoderBlock(in_channels=filters[3],
                                     n_filters=filters[2])
        self.decoder2 = DecoderBlock(in_channels=filters[2],
                                     n_filters=filters[1])
        self.decoder1 = DecoderBlock(in_channels=filters[1],
                                     n_filters=filters[0])
        self.final_layer = nn.Sequential(
            Conv1dSamePadding(filters[0], filters[0], 3),
            nonlinearity(inplace=True),
            Conv1dSamePadding(filters[0], filters[0], 2)
        )

    def forward(self,
                e1, e2, e3, e4):
        # make proper paddings here
        d3 = self.decoder3(e4)[:,:,:e3.size(2)] + e3
        d2 = self.decoder2(d3)[:,:,:e2.size(2)] + e2
        d1 = self.decoder1(d2)[:,:,:e1.size(2)] + e1
        out = self.final_layer(d1)
        # print(e1.size(), e2.size(), e3.size(), e4.size(), out.size())
        return out


class NaiveDenoising(nn.Module):
    def __init__(self,
                 filters=[161, 768, 768, 768], # am states
                 denoise_width=768,
                 denoise_depth=4,
                 dropout=0.1,
                 kernel_size=7
                ):
        super().__init__()

        self.tiling = TilingBlock(repeats=[2, 4, 8])

        bnorm = True
        input_shape = filters[0] * 4
        padding = kernel_size // 2

        block = SeparableRepeatBlock
        # "prolog"
        modules = [block(_in=input_shape, out=denoise_width, kernel_size=kernel_size,
                         padding=padding, dropout=dropout)]

        # main convs
        for _ in range(0, denoise_depth):
            modules.extend(
                [block(_in=denoise_width, out=denoise_width, kernel_size=kernel_size,
                       padding=padding, dropout=dropout, se_ratio=0.2)]
            )
        self.conv = nn.Sequential(*modules)

        self.compress_2 = nn.Sequential(*self.block(filters[1], filters[0], 1,
                                        padding=0, stride=1, bnorm=True,
                                        dropout=dropout))
        self.compress_3 = nn.Sequential(*self.block(filters[2], filters[0], 1,
                                        padding=0, stride=1, bnorm=True,
                                        dropout=dropout))
        self.compress_4 = nn.Sequential(*self.block(filters[3], filters[0], 1,
                                        padding=0, stride=1, bnorm=True,
                                        dropout=dropout))
        # shared "classifier"
        self.fc = nn.Sequential(
            nn.Conv1d(in_channels=denoise_width,
                      out_channels=filters[0],
                      kernel_size=1)
        )

    @staticmethod
    def block(in_channels, out_channels, kernel_size,
               padding=0, stride=1, bnorm=False, bias=True,
               dropout=0):

        res = [nn.Conv1d(in_channels=in_channels, out_channels=out_channels,
                         kernel_size=kernel_size, padding=padding,
                         stride=stride, bias=bias)]

        res.append(nn.BatchNorm1d(out_channels))
        res.append(nn.ReLU(inplace=True))
        res.append(nn.Dropout(dropout))
        return res

    def forward(self,
                e1, e2, e3, e4):

        (e2_tiled,
         e3_tiled,
         e4_tiled) = self.tiling(e1,
                                 (self.compress_2(e2),
                                  self.compress_3(e3),
                                  self.compress_4(e4)))
        x = torch.cat([e1,
                       e2_tiled,
                       e3_tiled,
                       e4_tiled], dim=1)
        x = self.conv(x)
        out = self.fc(x)
        return out


class ShortLinkNetDenoising(nn.Module):
    def __init__(self,
                 filters=[161, 768], # am states
                 nonlinearity=nn.ReLU
                ):
        super().__init__()

        self.decoder1 = DecoderBlock(in_channels=filters[1],
                                     n_filters=filters[0])
        self.final_layer = nn.Sequential(
            Conv1dSamePadding(filters[0], filters[0], 3),
            nonlinearity(inplace=True),
            Conv1dSamePadding(filters[0], filters[0], 2)
        )

    def forward(self,
                e1, e2):
        # make proper paddings here
        d1 = self.decoder1(e2)[:,:,:e1.size(2)] + e1
        out = self.final_layer(d1)
        return out


class BahdanauAttention(nn.Module):
    """Implements Bahdanau (MLP) attention"""

    def __init__(self,
                 hidden_size,
                 key_size=None,
                 query_size=None):
        super(BahdanauAttention, self).__init__()

        # We assume a non bi-directional encoder so key_size is 1*hidden_size
        key_size = 1 * hidden_size if key_size is None else key_size
        query_size = hidden_size if query_size is None else query_size

        self.key_layer = nn.Linear(key_size, hidden_size, bias=False)
        self.query_layer = nn.Linear(query_size, hidden_size, bias=False)
        self.energy_layer = nn.Linear(hidden_size, 1, bias=False)

        # to store attention scores
        self.alphas = None

    def forward(self,
                query=None,
                proj_key=None,
                value=None,
                mask=None):
        assert mask is not None, "mask is required"

        # We first project the query (the decoder state).
        # The projected keys (the encoder states) were already pre-computated.
        query = self.query_layer(query)

        # Calculate scores.
        scores = self.energy_layer(torch.tanh(query + proj_key))
        scores = scores.squeeze(2).unsqueeze(1)

        # Mask out invalid positions.
        # The mask marks valid positions so we invert it using `mask & 0`.
        scores.data.masked_fill_(mask == 0, -float('inf'))

        # Turn scores to probabilities.
        alphas = F.softmax(scores, dim=-1)
        self.alphas = alphas

        # The context vector is the weighted sum of the values.
        context = torch.bmm(alphas, value)

        # context shape: [B, 1, 2D], alphas shape: [B, 1, M]
        return context, alphas


class Generator(nn.Module):
    """Define standard linear + softmax generation step."""
    def __init__(self, hidden_size, vocab_size):
        super(Generator, self).__init__()
        self.proj = nn.Linear(hidden_size, vocab_size, bias=False)

    def forward(self, x):
        return F.log_softmax(self.proj(x), dim=-1)


class Decoder(nn.Module):
    """A conditional RNN decoder with attention."""

    def __init__(self,
                 emb_size, hidden_size, tgt_vocab, attention,
                 num_encoder_layers=2,
                 num_decoder_layers=2,
                 dropout=0.1,
                 sos_index=299):
        super(Decoder, self).__init__()

        self.dropout = dropout
        self.attention = attention
        self.tgt_vocab = tgt_vocab
        self.sos_index = sos_index
        self.num_decoder_layers = num_decoder_layers
        self.num_encoder_layers = num_encoder_layers
        self.hidden_size = hidden_size

        self.trg_embed = nn.Embedding(tgt_vocab, emb_size)
        self.generator = Generator(hidden_size, tgt_vocab)

        self.rnn = nn.GRU(emb_size + hidden_size,
                          hidden_size,
                          num_decoder_layers,
                          batch_first=True,
                          dropout=dropout)

        # to initialize from the final encoder state
        # self.bridge = nn.Linear(2*hidden_size, hidden_size, bias=True) if bridge else None

        # use CNN encoded states to initialize final encoder state
        self.bridge = nn.GRU(hidden_size,
                             hidden_size,
                             num_encoder_layers,
                             batch_first=True,
                             dropout=dropout)

        self.dropout_layer = nn.Dropout(p=dropout)
        self.pre_output_layer = nn.Linear(hidden_size + hidden_size + emb_size,
                                          hidden_size, bias=False)

    def forward_step(self, prev_embed, encoder_hidden, src_mask, proj_key, hidden):
        """Perform a single decoder step (1 word)"""
        # compute context vector using attention mechanism
        query = hidden[-1].unsqueeze(1)  # [#layers, B, D] -> [B, 1, D]

        context, attn_probs = self.attention(
            query=query, proj_key=proj_key,
            value=encoder_hidden, mask=src_mask
        )

        # update rnn hidden state
        rnn_input = torch.cat([prev_embed, context], dim=2)

        output, hidden = self.rnn(rnn_input, hidden)

        pre_output = torch.cat([prev_embed, output, context], dim=2)
        pre_output = self.dropout_layer(pre_output)
        pre_output = self.pre_output_layer(pre_output)

        return output, hidden, pre_output

    def forward(self,
                cnn_states,
                trg=None):
        if self.training:
            return self.train_batch(cnn_states,
                                    trg)
        else:
            return self.inference(cnn_states)

    def train_batch(self,
                    cnn_states,
                    trg):
        """Unroll the decoder one step at a time."""
        device = cnn_states.device

        src_mask = torch.ones(cnn_states.size(0),
                              cnn_states.size(1)).unsqueeze(1).to(device)    # .type_as(cnn_states)
        # during train, max iterations
        # is limited by teacher forcing
        max_len = trg.size(1)

        trg_embed = self.trg_embed(trg)
        encoder_output, encoder_hidden = self.init_rnn_states(cnn_states)

        # initialize decoder hidden state
        hidden = encoder_hidden

        # pre-compute projected encoder hidden states
        # (the "keys" for the attention mechanism)
        # this is only done for efficiency
        proj_key = self.attention.key_layer(encoder_output)

        # here we store all intermediate hidden states and pre-output vectors
        #decoder_states = []
        pre_output_vectors = []
        # print(max_len, trg_embed.size(), trg)
        # unroll the decoder RNN for max_len steps
        for i in range(max_len):
            prev_embed = trg_embed[:, i].unsqueeze(1)
            output, hidden, pre_output = self.forward_step(
                prev_embed, encoder_output, src_mask, proj_key, hidden)
            #decoder_states.append(output)
            pre_output_vectors.append(pre_output)

        #decoder_states = torch.cat(decoder_states, dim=1)
        pre_output_vectors = torch.cat(pre_output_vectors, dim=1)
        output = self.generator(pre_output_vectors)
        return output

    def inference(self, cnn_states):
        device = cnn_states.device

        batch_size = cnn_states.size(0)
        src_mask = torch.ones(cnn_states.size(0),
                              cnn_states.size(1)).unsqueeze(1).to(device)  # .type_as(cnn_states)
        # during inference, max iterations
        # for very fast speech may be 1 grapheme per window
        max_len = cnn_states.size(1)

        encoder_output, encoder_hidden = self.init_rnn_states(cnn_states)
        hidden = encoder_hidden

        # initial state with sos indices
        trg = torch.ones(batch_size, 1).fill_(self.sos_index).long().to(device)     # .type_as(cnn_states)
        trg_mask = torch.ones_like(trg).to(device)   #  .type_as(cnn_states)

        proj_key = self.attention.key_layer(encoder_output)

        # here we store all intermediate hidden states and pre-output vectors
        #decoder_states = []
        pre_output_vectors = []

        # unroll the decoder RNN for max_len steps
        for i in range(max_len):
            prev_embed = self.trg_embed(trg)

            output, hidden, pre_output = self.forward_step(
                prev_embed, encoder_output, src_mask, proj_key, hidden)
            #decoder_states.append(output)
            pre_output_vectors.append(pre_output)
            # we predict from the pre-output layer, which is
            # a combination of Decoder state, prev emb, and context
            prob = self.generator(pre_output[:, -1])

            _, next_word = torch.max(prob, dim=1)
            # next_word = next_word.data
            next_word = next_word.to(device)
            trg = next_word.unsqueeze(dim=1)

        #decoder_states = torch.cat(decoder_states, dim=1)
        pre_output_vectors = torch.cat(pre_output_vectors, dim=1)

        output = self.generator(pre_output_vectors)
        # for unification and simplicity, add a 100% probability
        # that first token is sos token
        sos_prob = torch.zeros_like(output[:,0:1,:]).to(device)
        sos_prob[:, 0, self.sos_index] = 1.0
        output = torch.cat([sos_prob, output], dim=1)
        return output

    def init_rnn_states(self, cnn_states):
        output, final = self.bridge(cnn_states)
        return output, final


def main():
    import os.path
    import argparse
    parser = argparse.ArgumentParser(description='DeepSpeech model information')
    parser.add_argument('--model-path', default='models/deepspeech_final.pth',
                        help='Path to model file created by training')
    args = parser.parse_args()
    package = torch.load(args.model_path, map_location=lambda storage, loc: storage)
    model = DeepSpeech.load_model(args.model_path)
    print("Model name:         ", os.path.basename(args.model_path))
    print("DeepSpeech version: ", model._version)
    print("")
    print("Recurrent Neural Network Properties")
    print("  RNN Type:         ", model._rnn_type)
    print("  RNN Layers:       ", model._hidden_layers)
    print("  RNN Size:         ", model._hidden_size)
    print("  Classes:          ", len(model._labels))
    print("")
    print("Model Features")
    print("  Labels:           ", model._labels)
    print("  Sample Rate:      ", model._audio_conf.get("sample_rate", "n/a"))
    print("  Window Type:      ", model._audio_conf.get("window", "n/a"))
    print("  Window Size:      ", model._audio_conf.get("window_size", "n/a"))
    print("  Window Stride:    ", model._audio_conf.get("window_stride", "n/a"))
    if package.get('loss_results', None) is not None:
        print("")
        print("Training Information")
        epochs = package['epoch']
        print("  Epochs:           ", epochs)
        print("  Current Loss:      {0:.3f}".format(package['loss_results'][epochs - 1]))
        print("  Current CER:       {0:.3f}".format(package['cer_results'][epochs - 1]))
        print("  Current WER:       {0:.3f}".format(package['wer_results'][epochs - 1]))
    if package.get('meta', None) is not None:
        print("")
        print("Additional Metadata")
        for k, v in model._meta:
            print("  ", k, ": ", v)


if __name__ == '__main__':
    main()
