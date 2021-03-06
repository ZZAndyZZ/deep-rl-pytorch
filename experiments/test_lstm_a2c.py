import gym
import random
import numpy as np

import torch
import torch.nn as nn

from deep_rl import register_trainer, make_trainer
from deep_rl.a2c import A2CTrainer as Trainer, create_vec_envs
from deep_rl.a2c.model import LSTMMultiLayerPerceptron, TimeDistributed
from deep_rl.common.pytorch import forward_masked_rnn_transposed
from deep_rl.common.env import RewardCollector

from deep_rl.model import TimeDistributed, Flatten, MaskedRNN

class Model(nn.Module):
    def __init__(self, action_space_size):
        super().__init__()
        
        self.rnn = MaskedRNN(nn.LSTM(action_space_size, 
            hidden_size = 16, 
            num_layers = 1,
            batch_first = True))

        self.actor = TimeDistributed(nn.Linear(16, action_space_size))
        self.critic = TimeDistributed(nn.Linear(16, 1))

    def initial_states(self, batch_size):
        return tuple([torch.zeros([batch_size, 1, 16], dtype = torch.float32) for _ in range(2)])

    def forward(self, inputs, masks, states):
        features, states = self.rnn(inputs, masks, states)
        return self.actor(features), self.critic(features), states

class TestLstm(gym.Env):
    def __init__(self):
        self.action_space = gym.spaces.Discrete(4)
        self.observation_space = gym.spaces.Box(0.0, 1.0, shape=(4,))
        self.random = random.Random()
        self.length = 5

    def seed(self, seed = None):
        self.random.seed(seed)

    def reset(self):
        self.time = 0
        self.chosen = 1 + self.random.randrange(self.action_space.n - 1)
        return self.observe()

    def step(self, action):
        self.time += 1
        if self.time != self.length:
            if action == 0:
                return self.observe(), 0.0, False, dict()
            else:
                return self.observe(), 0.0, True, dict()
        else:
            if action == self.chosen:
                return self.observe(), 1.0, True, dict()
            else:
                return self.observe(), 0.0, True, dict()

    def observe(self):
        r = np.zeros((self.action_space.n,), dtype = np.float32)
        if self.time == 0:
            r[self.chosen] = 1.0
        return r

gym.register(
    id = 'lstm-v1',
    entry_point = 'experiments.test_lstm_a2c:TestLstm'
)

@register_trainer(episode_log_interval = 100, save = False)
class SomeTrainer(Trainer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.num_steps = 10
        self.allow_gpu = False

    def create_env(self, env_kwargs):
        def thunk():
            return RewardCollector(gym.make(**env_kwargs))
            
        env, self.validation_env = create_vec_envs(thunk, self.num_processes)
        return env

    def create_model(self, **model_kwargs):
        observation_space = self.env.observation_space
        action_space_size = self.env.action_space.n
        return Model(action_space_size)

def default_args():
    return dict(
        model_kwargs = dict(),
        env_kwargs = dict(id = 'lstm-v1')
    )
