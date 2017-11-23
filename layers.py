'''
SMASH Layers

Andy Brock, 2017

This code contains the layer definitions for SMASH networks and derivative
networks as described in my paper,
"One-Shot Model Architecture Search through HyperNetworks."

This code is thoroughly commented throughout, but is still rather complex.
If there's something that's unclear please feel free to ask and I'll do my best
to explain it or update the comments to better describe what's going on.
'''
import sys
import math
import numpy as np
from collections import OrderedDict

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.init as init
import torch.nn.functional as F
from torch.nn import Parameter as P
from torch.autograd import Variable as V


# Softmax helper function; I use this to normalize a numpy array for
# use with np.random.choice to give properly scaled probabilities.

def softmax(x):
    e_x = np.exp(x - np.max(x))
    return e_x / e_x.sum()


# Elementwise-Sum Layer: this is a simple wrapper in the spirit of Lasagne that
# is useful for designing ResNet with the module interface, rather than having
# to make use of a needlessly complex forward() function.
class ESL(nn.Module):
    def __init__(self, module):
        super(ESL, self).__init__()
        self.module = module

    def forward(self, x):
        return x + self.module(x)

# Elementwise Mult Layer: Similar to ESL, but for multiplication.
class EML(nn.Module):
    def __init__(self, module):
        super(ESL, self).__init__()
        self.module = module

    def forward(self, x):
        return x * self.module(x)

# Channel-wise Concatenation Layer: Similar to ESL, but for DenseNets.
class CL(nn.Module):
    def __init__(self, module):
        super(CL, self).__init__()
        self.module = module

    def forward(self, x):
        return torch.cat((x, self.module(x)), 1)

# Apply simplified weightnorm to a 2d conv filter      
def wn2d(w):
    return w / torch.norm(w).view(1,1,1,1).expand_as(w)

    
    
# 2D Convolution with Simple WeightNorm.
# As described in the paper, I found that standard WeightNorm
# (normalizing channel-by-channel and having an additional scale parameter)
# was unstable, but that simply dividing the weight by the entire tensor's norm
# worked well. I use this primarily in the definition of the HyperNet.
class WNC2D(nn.Conv2d):
    def forward(self, input):
        return F.conv2d(input,
                        wn2d(self.weight),
                        self.bias,
                        self.stride,
                        self.padding,
                        self.dilation,
                        self.groups)

        
# Simple class that dynamically inserts a nonlinearity between a batchnorm and a conv                        
class seq(nn.Module):
    def __init__(self, n_in, n_out, kernel_size=(3,3), dilation=(1,1), preactivation=True, batchnorm=False, groups=1, activation=F.relu):
        super(seq, self).__init__()
        
        self.dilation = dilation
        
        # Whether to use pre or post activation  
        self.preactivation = preactivation
        
        self.batchnorm = batchnorm
        
        if self.batchnorm:
            self.bn = nn.BatchNorm2d(n_in)
            
        self.conv = nn.Conv2d(int(n_in), 
          int(n_out), 
          kernel_size=tuple(int(ks) for ks in kernel_size),
          padding=tuple(int(item) for item in ( (kernel_size[0] + ((kernel_size[0] - 1 ) * (dilation[0] - 1 ))) // 2, (kernel_size[1] + ((kernel_size[1] - 1 ) * (dilation[1] - 1 ))) // 2)),
          dilation=tuple(int(d) for d  in dilation),
          groups=int(groups),
          bias=False)
        
        
        # Activation function, currently deprecated
        self.activation = activation
        
    def forward(self, x, f=F.relu):
        # If using preactivation, (BN)-NL-CONV
        if self.preactivation:
            if self.batchnorm:
                return self.conv(f(self.bn(x)))
            else:
                return self.conv(f(x))
        
        # If using standard activation, CONV-(BN)-NL
        else:
            if self.batchnorm:
                return f(self.bn(self.conv(x)))
            else:
                return f(self.conv(x))
    

          

# A single layer for use with derivative networks.
# This module defines a fixed-structure layer and is compatible with
# the output of SMASH.sample_architecture().
# It presently only supports ReLU activations and gating, though the
# SMASH network supports variable activations.
# Probably want n_bottleneck too...
# norm style supports "before," where the we only batch-normalize the incoming
# read tensor, "sandwich," where we batch-normalize the input and the output
# of the 1x1, and "full," where we individually batch-normalize the input to
# each convolution, or WN, where we just normalize the 1x1 as in our SMASH net.
class Layer(nn.Module):
    def __init__(self, n_in, n_bottle, n_out, ops, gate, dilation=[(1,1)]*4, activation=[F.relu]*4, kernel_size=[(3,3)]*4, groups=[1]*4, preactivation=True, gate_style='add_split',norm_style='sandwich'):
        super(Layer, self).__init__()
        # The number of incoming channels
        self.n_in = n_in
        # The number of output channels for the 1x1 conv
        self.n_bottle = n_bottle
        # The final number of outgoing channels
        self.n_out = n_out
        # The list defining which ops are active
        self.ops = ops
        # Which gates are active
        self.gate = gate
        # Dilation factor
        self.dilation = dilation
        # Activation functions
        self.activation = activation
        # Kernel_size
        self.kernel_size = kernel_size
        # Pre or post activation
        self.preactivation = preactivation
        # gate style, from mult or add_split
        self.gate_style = gate_style
        # norm style, from before, sandwich, full, or wn
        self.norm_style = norm_style
        # Initial batchnorm and conv
        
        if self.norm_style != 'WN':
            self.bn1 = nn.BatchNorm2d(self.n_in if self.preactivation else self.n_bottle)
            self.conv1 = nn.Conv2d(self.n_in, self.n_bottle, 
                                   kernel_size=1, bias=False)
            if self.preactivation:
                self.initial_op = nn.Sequential(self.bn1,nn.ReLU(),self.conv1)
            else:
                self.initial_op = nn.Sequential(self.conv1,self.bn1, nn.ReLU())
            
        else:
            self.conv1 = WNC2D(self.n_in, self.n_bottle, 
                                   kernel_size=1, bias=False)
            if self.preactivation:
                self.initial_op = nn.Sequential(nn.ReLU(),self.conv1)
            else:
                self.initial_op = nn.Sequential(self.conv1,nn.ReLU())
        
        if self.norm_style == 'sandwich':  
            self.bn2 = nn.BatchNorm2d(self.n_bottle)
            self.initial_op.add_module('3',self.bn2)
            
        # Op list, not to be confused with ops.
        self.op = nn.ModuleList()
        
        # Use batchnorm in sequence?
        self.seq_bn = True if self.norm_style =='full' else False
        
        for i, o in enumerate(ops):
            if o:
                self.op.append(seq(n_in=n_out if i%2 else n_bottle,
                                   n_out=n_out,
                                   dilation=self.dilation[i],
                                   kernel_size=kernel_size[i],
                                   preactivation=self.preactivation,
                                   batchnorm=self.seq_bn,
                                   groups=groups[i],
                                   activation=self.activation[i]))
                   
            else:
                self.op.append(nn.Module())


    
    # See SMASHLAYER for an explanation of the flow control here.
    def forward(self, x):
    
        out = self.initial_op(x)        
        
        if self.gate[0]:
            if self.gate_style == 'mult':
                out = self.op[0](out,F.tanh) * self.op[2](out,F.sigmoid)
            else:
                pre_gate = out = self.op[0](out) + self.op[2](out)
                out = F.tanh(pre_gate[:,::2]) * F.sigmoid(pre_gate[:,1::2])
            
        elif type(self.op[2]) is seq:
            out = [self.op[0](out), self.op[2](out)]
        
        else:
            out = self.op[0](out)
        
        if self.gate[1]:
            if type(out) is list:
                if self.gate_style == 'mult':
                     out = self.op[1](out[0],F.tanh) * self.op[3](out[1],F.sigmoid)
                else:
                    pre_gate =  out = self.op[1](out[0]) + self.op[3](out[1])
                    out = F.tanh(pre_gate[:,::2]) * F.sigmoid(pre_gate[:,1::2])
            
            # If we only have one path incoming then read from it
            else:
                if self.gate_style == 'mult':
                    out = self.op[1](out,F.tanh) * self.op[3](out,F.sigmoid)
                else:
                   pre_gate =  out = self.op[1](out) + self.op[3](out)
                   out = F.tanh(pre_gate[:,::2]) * F.sigmoid(pre_gate[:,1::2])


        elif type(self.op[3]) is seq:
            if type(out) is list:
                out = self.op[1](out[0]) + self.op[3](out[1])
            else:
                out = self.op[1](out) + self.op[3](out)
        
        elif type(self.op[1]) is seq:
            if type(out) is list:
                out = self.op[1](out[0]) + out[1]
            else:
                out = self.op[1](out)
        
        elif type(out) is list:
            out = out[0] + out[1]
            
        return out

        
        
 
# A transition module, borrowed from DenseNet-BC.
# This module uses BatchNorm, followed by a 1x1 convolution and then
# average pooling with a pooling size of 2 to perform downsampling.


class Transition(nn.Module):
    def __init__(self, nChannels, nOutChannels):
        super(Transition, self).__init__()
        self.bn1 = nn.BatchNorm2d(nChannels)
        self.conv1 = nn.Conv2d(nChannels, nOutChannels, kernel_size=1,
                               bias=False)

    def forward(self, x):
        out = self.conv1(F.relu(self.bn1(x)))
        out = F.avg_pool2d(out, 2)
        return out

# Simple multiscale dilated conv block that uses masks. Note that using this block
# will mess up the parameter count. You could do this less efficiently by using the
# masks to write the weight tensor to the locations in a variable at each point in the graph,
# but I find the masks to just be faster.                        
class MDC(nn.Module):
    def __init__(self, n_in,n_out, dilation):
        super(MDC, self).__init__()
        self.dilation = dilation
        
        if self.dilation==2:
            self.m = torch.FloatTensor( [ [ [ [1,0,1,0,1],
                                              [0,1,1,1,0],
                                              [1,1,1,1,1],
                                              [0,1,1,1,0],
                                              [1,0,1,0,1]]]*(n_in)]*n_out).cuda()
        elif self.dilation==3:
            self.m = torch.FloatTensor( [ [ [ [1,0,0,1,0,0,1],
                                              [0,1,0,1,0,1,0],
                                              [0,0,1,1,1,0,0],
                                              [1,1,1,1,1,1,1],
                                              [0,0,1,1,1,0,0],
                                              [0,1,0,1,0,1,0],
                                              [1,0,0,1,0,0,1]]]*(n_in)]*n_out).cuda()
        self.conv = nn.Conv2d(n_in,n_out,kernel_size=3+2*(self.dilation-1),
                              padding=self.dilation,dilation=self.dilation, bias=False)
    def forward(self,x):
        if self.dilation>1:
            return F.conv2d(input = x,weight=self.conv.weight*V(self.m),padding=self.dilation,bias=None)
        else:
            return self.conv(x)
            
