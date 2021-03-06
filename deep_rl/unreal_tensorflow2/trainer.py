# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import numpy as np
import random
import time
import sys

from .environment.environment import Environment
from .model.model import UnrealModel
from .train.experience import Experience, ExperienceFrame
from ..a2c_unreal.storage import ExperienceReplay
from ..common.storage import split_batched_items

from ..common.schedules import LinearSchedule

from ..a2c_unreal.util import pixel_control_reward

class A3CAgent(object):
    def __init__(self,
                    sess,
                 thread_index,
                 global_network,
                 initial_learning_rate,
                 learning_rate_input,
                 grad_applier,
                 env_type,
                 env_name,
                 use_pixel_change,
                 use_value_replay,
                 use_reward_prediction,
                 pixel_change_lambda,
                 entropy_beta,
                 local_t_max,
                 gamma,
                 gamma_pc,
                 experience_history_size,
                 max_global_time_step,
                 device):

        self._global_t = None
        self.sess = sess

        self.thread_index = thread_index
        self.learning_rate_input = learning_rate_input
        self.env_type = env_type
        self.env_name = env_name
        self.environment = None
        self.use_pixel_change = use_pixel_change
        self.use_value_replay = use_value_replay
        self.use_reward_prediction = use_reward_prediction
        self.local_t_max = local_t_max
        self.gamma = gamma
        self.gamma_pc = gamma_pc
        self.experience_history_size = experience_history_size
        self.max_global_time_step = max_global_time_step
        self.action_size = Environment.get_action_size(env_type, env_name)

        self.local_network = UnrealModel(self.action_size,
                                         thread_index,
                                         use_pixel_change,
                                         use_value_replay,
                                         use_reward_prediction,
                                         pixel_change_lambda,
                                         entropy_beta,
                                         device)
        self.local_network.prepare_loss()

        self.apply_gradients = grad_applier.minimize_local(self.local_network.total_loss,
                                                           global_network.get_vars(),
                                                           self.local_network.get_vars())

        self.sync = self.local_network.sync_from(global_network)
        self.experience = ExperienceReplay(self.experience_history_size, self.local_t_max)
        self.local_t = 0

        self.learning_rate_schedule = LinearSchedule(7e-4, 0.0, max_global_time_step)
        self.learning_rate_schedule.step(0)

        self.initial_learning_rate = 7e-4# initial_learning_rate # self.learning_rate_schedule()
        self.episode_reward = 0
        self.episode_length = 0
        # For log output
        self.prev_local_t = 0

        self._last_state = None

    def _anneal_learning_rate(self, global_time_step):
        learning_rate = self.initial_learning_rate * \
            (self.max_global_time_step - global_time_step) / \
            self.max_global_time_step
        if learning_rate < 0.0:
            learning_rate = 0.0
        return learning_rate

    def run(self, process, **kwargs):
        self._global_t = 0
        self._is_stopped = False

        while not self._is_stopped:
            tdiff, _, _ = process(mode = 'train', context = dict())
            self._global_t += tdiff

        return None
        

    def initialize(self):
        self.environment = self.create_env()

    def stop(self):
        self.environment.stop()

    def choose_action(self, pi_values):
        return np.random.choice(range(len(pi_values)), p=pi_values)

    def _fill_experience(self):
        """
        Fill experience buffer until buffer is full.
        """
        if self._last_state is None:
            self._last_state = self.environment.reset()

        pi_, _ = self.local_network.run_base_policy_and_value(self.sess, *self._last_state)
        action = self.choose_action(pi_)

        new_state, reward, terminal, stats = self.environment.step(action)
        self._last_state = new_state

        self.experience.insert(self._last_state, action, reward, terminal)
        

        if terminal:
            self._last_state = self.environment.reset()
        if self.experience.full:
            self._last_state = self.environment.reset()
            print("Replay buffer filled")

    def _process_base(self):
        epend = None
        # [Base A3C]
        states = []
        last_action_rewards = []
        actions = []
        rewards = []
        values = []

        terminal_end = False

        start_lstm_state = self.local_network.base_lstm_state_out

        # t_max times loop
        for _ in range(self.local_t_max):
            # Prepare last action reward
            observation, last_action_reward = self._last_state

            pi_, value_ = self.local_network.run_base_policy_and_value(self.sess, *self._last_state)

            action = self.choose_action(pi_)

            states.append(observation)
            last_action_rewards.append(last_action_reward)
            actions.append(action)
            values.append(value_)

            prev_state = self._last_state

            # Process game
            new_state, reward, terminal, stats = self.environment.step(action)
            self._last_state = new_state

            # Store to experience
            self.experience.insert(prev_state, action, reward, terminal)

            self.episode_reward += reward
            self.episode_length += 1

            rewards.append(reward)

            self.local_t += 1

            if terminal:
                terminal_end = True
                epend = (self.episode_length, self.episode_reward)              

                self.episode_reward = 0
                self.episode_length = 0
                self._last_state = self.environment.reset()
                self.local_network.reset_state()
                break

        R = 0.0
        if not terminal_end:
            R = self.local_network.run_base_value(
                self.sess, *self._last_state)

        actions.reverse()
        states.reverse()
        rewards.reverse()
        values.reverse()

        batch_si = []
        batch_a = []
        batch_adv = []
        batch_R = []

        for(ai, ri, si, Vi) in zip(actions, rewards, states, values):
            R = ri + self.gamma * R
            adv = R - Vi
            a = np.zeros([self.action_size])
            a[ai] = 1.0

            batch_si.append(si)
            batch_a.append(a)
            batch_adv.append(adv)
            batch_R.append(R)

        batch_si.reverse()
        batch_a.reverse()
        batch_adv.reverse()
        batch_R.reverse()

        return batch_si, last_action_rewards, batch_a, batch_adv, batch_R, start_lstm_state, epend

    def _subsample(self, a, average_width):
        s = a.shape
        sh = s[0]//average_width, average_width, s[1]//average_width, average_width
        return a.reshape(sh).mean(-1).mean(1)  

    def _calc_pixel_change(self, state, last_state):
        d = np.absolute(state[2:-2,2:-2,:] - last_state[2:-2,2:-2,:])
        # (80,80,3)
        m = np.mean(d, 2)
        c = self._subsample(m, 4)
        return c

    def _process_pc(self):
        # [pixel change]
        pc_experience_frames = split_batched_items(self.experience.sample_sequence())
        # Revese sequence to calculate from the last
        pc_experience_frames.reverse()

        batch_pc_si = []
        batch_pc_a = []
        batch_pc_R = []
        batch_pc_last_action_reward = []

        pc_R = np.zeros([20, 20], dtype=np.float32)
        if not pc_experience_frames[1][3]:
            pc_R = self.local_network.run_pc_q_max(self.sess, *pc_experience_frames[0][0])

        for i, frame in enumerate(pc_experience_frames[1:]):
            pixel_change = self._calc_pixel_change(pc_experience_frames[i][0][0], frame[0][0])
            pc_R = pixel_change + self.gamma_pc * pc_R
            a = np.zeros([self.action_size])
            a[frame[1]] = 1.0
            last_action_reward = frame[0][1]

            batch_pc_si.append(frame[0][0])
            batch_pc_a.append(a)
            batch_pc_R.append(pc_R)
            batch_pc_last_action_reward.append(last_action_reward)

        batch_pc_si.reverse()
        batch_pc_a.reverse()
        batch_pc_R.reverse()
        batch_pc_last_action_reward.reverse()

        return batch_pc_si, batch_pc_last_action_reward, batch_pc_a, batch_pc_R

    def _process_vr(self):
        # [Value replay]
        # Sample 20+1 frame (+1 for last next state)
        vr_experience_frames = split_batched_items(self.experience.sample_sequence())
        # Revese sequence to calculate from the last
        vr_experience_frames.reverse()

        batch_vr_si = []
        batch_vr_R = []
        batch_vr_last_action_reward = []

        vr_R = 0.0
        if not vr_experience_frames[1][3]:
            vr_R = self.local_network.run_vr_value(self.sess, *vr_experience_frames[0][0])

        # t_max times loop
        for frame in vr_experience_frames[1:]:
            vr_R = frame[2] + self.gamma * vr_R
            batch_vr_si.append(frame[0][0])
            batch_vr_R.append(vr_R)
            batch_vr_last_action_reward.append(frame[0][1])

        batch_vr_si.reverse()
        batch_vr_R.reverse()
        batch_vr_last_action_reward.reverse()

        return batch_vr_si, batch_vr_last_action_reward, batch_vr_R

    def _process_rp(self):
        # [Reward prediction]
        rp_experience_frames = split_batched_items(self.experience.sample_rp_sequence())
        # 4 frames

        batch_rp_si = []
        batch_rp_c = []

        for i in range(3):
            batch_rp_si.append(rp_experience_frames[i][0][0])

        # one hot vector for target reward
        r = rp_experience_frames[3][2]
        rp_c = [0.0, 0.0, 0.0]
        if r == 0:
            rp_c[0] = 1.0  # zero
        elif r > 0:
            rp_c[1] = 1.0  # positive
        else:
            rp_c[2] = 1.0  # negative
        batch_rp_c.append(rp_c)
        return batch_rp_si, batch_rp_c

    def process(self, mode = 'train', **kwargs):
        # Fill experience replay buffer
        if not self.experience.full:
            self._fill_experience()
            return 0, None, dict()

        start_local_t = self.local_t

        self.learning_rate_schedule.step(self._global_t)
        cur_learning_rate = self.learning_rate_schedule()

        # Copy weights from shared to local
        self.sess.run(self.sync)

        # [Base]
        batch_si, batch_last_action_rewards, batch_a, batch_adv, batch_R, start_lstm_state, epend = \
            self._process_base()
        feed_dict = {
            self.local_network.base_input: batch_si,
            self.local_network.base_last_action_reward_input: batch_last_action_rewards,
            self.local_network.base_a: batch_a,
            self.local_network.base_adv: batch_adv,
            self.local_network.base_r: batch_R,
            self.local_network.base_initial_lstm_state: start_lstm_state,
            # [common]
            self.learning_rate_input: cur_learning_rate
        }

        # [Pixel change]
        if self.use_pixel_change:
            batch_pc_si, batch_pc_last_action_reward, batch_pc_a, batch_pc_R = self._process_pc()
            pc_feed_dict = {
                self.local_network.pc_input: batch_pc_si,
                self.local_network.pc_last_action_reward_input: batch_pc_last_action_reward,
                self.local_network.pc_a: batch_pc_a,
                self.local_network.pc_r: batch_pc_R
            }
            feed_dict.update(pc_feed_dict)

        # [Value replay]
        if self.use_value_replay:
            batch_vr_si, batch_vr_last_action_reward, batch_vr_R = self._process_vr()

            vr_feed_dict = {
                self.local_network.vr_input: batch_vr_si,
                self.local_network.vr_last_action_reward_input: batch_vr_last_action_reward,
                self.local_network.vr_r: batch_vr_R
            }
            feed_dict.update(vr_feed_dict)

        # [Reward prediction]
        if self.use_reward_prediction:
            batch_rp_si, batch_rp_c = self._process_rp()
            rp_feed_dict = {
                self.local_network.rp_input: batch_rp_si,
                self.local_network.rp_c_target: batch_rp_c
            }
            feed_dict.update(rp_feed_dict)

        # Calculate gradients and copy them to global netowrk.
        self.sess.run(self.apply_gradients, feed_dict=feed_dict)

        # Return advanced local step size
        diff_local_t = self.local_t - start_local_t
        return diff_local_t, epend, dict()
