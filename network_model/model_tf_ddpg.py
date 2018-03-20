import numpy as np
import tensorflow as tf
from network_model.model_tf import ModelRunnerTF, new_session
from network_model.model_tf import Model
import math
import copy
import random

def get_rewards(states):
    """Computes the rewards of states"""
    rewards = []

    for state in states:
        obs = np.squeeze(state)
        angle = obs[0]
        speedX = obs[21]
        trackPos = obs[20]

        reward = 0

        # car direction is reverse
        if abs(angle) > 3.14 / 2:
            reward = -100
        else:
            reward = speedX * abs(np.cos(angle)) - speedX * abs(np.sin(angle)) - 50 * abs(trackPos)

        rewards.append(reward)

    return rewards

def get_reward_derivatives(states):
    derivative = np.zeros(shape=(states.shape))
 
    for i, state in enumerate(states):
         angle = states[0][0][0]
         speedX = state[0][21][0]
         trackPos = state[0][20][0]
         
         if abs(angle) <= 3.14 / 2:
	     derivative[i][0][0][0] = -speedX * np.sin(angle) - speedX * np.cos(angle)
             derivative[i][0][21][0] = np.cos(angle) - np.sin(angle) - abs(trackPos)
             derivative[i][0][20][0] = -speedX

    return derivative

class ModelRunnerTFDdpg(ModelRunnerTF):
    def __init__(self, args,  action_group_no, thread_no):
        self.args = args
        learning_rate = args.learning_rate
        rms_decay = args.rms_decay
        rms_epsilon =  args.rms_epsilon
        self.network = args.network
        self.thread_no = thread_no

        self.train_batch_size = args.train_batch_size
        self.discount_factor = args.discount_factor
        self.action_group_no = action_group_no
        self.action_mat = np.zeros((self.train_batch_size, self.action_group_no))
        tf.logging.set_verbosity(tf.logging.WARN)
        self.output_path = "results-newcookie/"
        self.sess = new_session()
        self.init_models(self.network, action_group_no, learning_rate, rms_decay, rms_epsilon)

    
    def get_reward(self, state):
        obs = tf.squeeze(state)
        angle = obs[0]
        speedX = obs[21]
        trackPos = obs[20]

        def fn1():
            print('car direction reverse!')
            return tf.convert_to_tensor(-100, dtype=tf.float32)

        def fn2():
            print('car direction not reverse')
            return tf.convert_to_tensor(speedX * tf.abs(tf.cos(angle)) - speedX * tf.abs(tf.sin(angle)) - 50 * tf.abs(trackPos), dtype=tf.float32)

        # car direction is reverse
        return tf.cond(tf.abs(angle) > 3.14 / 2, fn1, fn2)
    
    def norm(self, x):
	return tf.nn.l2_loss(tf.squeeze(x))
 
    def add_summary(self):
        self.ep_score_placeholder = tf.placeholder(tf.float32, shape=(), name="ep_score")
        self.loss_placeholder = tf.placeholder(tf.float32, shape=(), name="loss")
        #self.grad_norm_placeholder = tf.placeholder(tf.float32, shape=(), name="grad_norm")
        self.min_critic_grad_placeholder = tf.placeholder(tf.float32, shape=(), name="min_critic_grad")
        self.max_critic_grad_placeholder = tf.placeholder(tf.float32, shape=(), name="max_critic_grad")
        self.l_r_placeholder = tf.placeholder(tf.float32, shape=(), name="l_r")


        tf.summary.scalar("ep_score", self.ep_score_placeholder)
        tf.summary.scalar("loss", self.loss_placeholder)
        #tf.summary.scalar("grad_norm", self.grad_norm_placeholder)
        tf.summary.scalar("min_critic_grad", self.min_critic_grad_placeholder)
        tf.summary.scalar("max_critic_grad", self.max_critic_grad_placeholder)
        tf.summary.scalar("l_r", self.l_r_placeholder)

        self.merged = tf.summary.merge_all()
        self.file_writer = tf.summary.FileWriter(self.output_path, self.sess.graph)

    def init_models(self, network, action_group_no, learning_rate, rms_decay, rms_epsilon):
        with tf.device(self.args.device):
            if self.args.env == 'torcs':
                if self.args.vision:
                    model_policy = ModelTorcsPixel(self.args, "policy", action_group_no, self.thread_no)
                    model_target = ModelTorcsPixel(self.args, "target", action_group_no, self.thread_no)
                else:
                    model_policy = ModelTorcsLowDim(self.args, "policy", action_group_no, self.thread_no)
                    model_target = ModelTorcsLowDim(self.args, "target", action_group_no, self.thread_no)
            else:
                raise ValueError('env %s is not supported.' % self.args.env)

            self.model_policy = model_policy

            self.x_in = model_policy.x_in
            self.action_in = model_policy.action_in
            self.actor_y = model_policy.actor_y
            self.critic_y = model_policy.critic_y
            self.vars = model_policy.variables
            self.actor_vars = model_policy.actor_vars

            self.x_in_target = model_target.x_in
            self.action_in_target = model_target.action_in
            self.actor_y_target = model_target.actor_y
            self.critic_y_target = model_target.critic_y
            self.vars_target = model_target.variables
            self.actor_vars_target = model_target.actor_vars

            # build the variable copy ops
            self.update_t = tf.placeholder(tf.float32, 1)
            self.update_target_list = []
            for i in range(0, len(self.vars)):
                self.update_target_list.append(self.vars_target[i].assign(self.update_t * self.vars[i] + (1-self.update_t) * self.vars_target[i]))
            self.update_target = tf.group(*self.update_target_list)

            self.critic_y_ = tf.placeholder(tf.float32, [None])
            self.critic_grads_in = tf.placeholder(tf.float32, [None, action_group_no])
            optimizer_critic = tf.train.AdamOptimizer(0.001)
            self.critic_grads = tf.gradients(self.critic_y, self.action_in)
            self.difference = tf.abs(self.critic_y_ - tf.reshape(self.critic_y, [-1]))
            quadratic_part = tf.clip_by_value(self.difference, 0.0, 1.0)
            linear_part = self.difference - quadratic_part
            self.errors = (0.5 * tf.square(quadratic_part)) + linear_part
            if self.args.prioritized_replay == True:
                self.priority_weight = tf.placeholder(tf.float32, shape=self.difference.get_shape(), name="priority_weight")
                self.errors = tf.mul(self.errors, self.priority_weight)
            self.loss = tf.reduce_sum(self.errors)
            self.critic_step = optimizer_critic.minimize(self.loss)
            self.loss_grad = tf.gradients(self.loss, self.x_in)

            optimizer_actor = tf.train.AdamOptimizer(0.0001)
            gvs = optimizer_actor.compute_gradients(self.actor_y, var_list=self.actor_vars, grad_loss=-1 * self.critic_grads_in)
            self.actor_step = optimizer_actor.apply_gradients(gvs)

	    # Set up the adversarial perturbation
            self.gamma = 1 # make it really big 

            self.saver = tf.train.Saver(max_to_keep=100)
            self.add_summary()
            self.sess.run(tf.initialize_all_variables())
            self.sess.run(self.update_target, feed_dict={
                self.update_t: [1.0]
            })

    def predict(self, history_buffer):
        return self.sess.run(self.actor_y, {self.x_in: history_buffer})[0]

    def print_weights(self):
        for var in self.vars:
            print ''
            print '[ ' + var.name + ']'
            print self.sess.run(var)

    def train(self, minibatch, replay_memory, learning_rate, debug, global_step_no):
        #global global_step_no

        if self.args.prioritized_replay == True:
            prestates, actions, rewards, poststates, terminals, replay_indexes, heap_indexes, weights = minibatch
        else:
            prestates, actions, rewards, poststates, terminals = minibatch

        actions_post = self.sess.run(self.actor_y_target, feed_dict={
                self.x_in_target: poststates
        })

	if global_step_no > 150000 and random.random() > 0.5:
	    perturbed_states = copy.deepcopy(poststates)
    	    for i in range(10):
	        alpha = .0001 / math.sqrt(i + 1)
	        perturbed_states -= alpha * (get_reward_derivatives(perturbed_states) + self.gamma * (perturbed_states - poststates)) 
            poststates = copy.deepcopy(perturbed_states)

	y2 = self.sess.run(self.critic_y_target, feed_dict={
            self.x_in_target: poststates,
            self.action_in_target: actions_post
        })

        y_ = np.zeros((self.train_batch_size))

        for i in range(self.train_batch_size):
            if self.args.clip_reward:
                reward = self.clip_reward(rewards[i])
            else:
                reward = rewards[i]
            if terminals[i]:
                y_[i] = reward
            else:
                y_[i] = reward + self.discount_factor * y2[i]

        if self.args.prioritized_replay == True:
            delta_value, _ = self.sess.run([self.difference, self.critic_step], feed_dict={
                self.x_in: prestates,
                self.action_in: actions,
                self.critic_y_: y_,
                self.priority_weight: weights
            })
            for i in range(self.train_batch_size):
                replay_memory.update_td(heap_indexes[i], abs(delta_value[i]))
                #if debug:
                #    print 'weight[%s]: %.5f, delta: %.5f, newDelta: %.5f' % (i, weights[i], delta_value[i], weights[i] * delta_value[i])
        else:
            self.sess.run([self.critic_step], feed_dict={
                self.x_in: prestates,
                self.action_in: actions,
                self.critic_y_: y_
            })

        actor_y_value = self.sess.run(self.actor_y, feed_dict={
            self.x_in: prestates,
        })

        self.critic_grads_value = self.sess.run(self.critic_grads, feed_dict= {
            self.x_in: prestates,
            self.action_in: actor_y_value
        })

        #if debug:
        #    print 'critic_grads_value : %s, %s' % (np.min(critic_grads_value), np.max(critic_grads_value))

        self.sess.run(self.actor_step, feed_dict={
            self.x_in: prestates,
            self.critic_grads_in: self.critic_grads_value[0]
        })
        self.loss_value = self.sess.run(self.loss, feed_dict={
            self.x_in: prestates,
            self.action_in: actions,
            self.critic_y_: y_
        })

    def update_model(self):
        self.sess.run(self.update_target, feed_dict={
            self.update_t: [0.001]
        })


class ModelTorcsLowDim(Model):
    def build_network(self, name, network, action_group_no):
        self.print_log('Building network for ModelTorcsLowDim')

        with tf.variable_scope(name):
            input_img_size = self.screen_height * self.screen_width * self.history_len
            x_in = tf.placeholder(tf.float32, shape=[None, self.screen_height, self.screen_width, self.history_len], name="screens")
            x_flat = tf.reshape(x_in, (-1, input_img_size))
            self.print_log(x_flat)

            # Actor network
            with tf.variable_scope('actor'):
                W_actor_fc1, b_actor_fc1 = self.make_layer_variables([input_img_size, 600], "actor_fc1")
                h_actor_fc1 = tf.nn.relu(tf.matmul(x_flat, W_actor_fc1) + b_actor_fc1, name="h_actor_fc1")
                self.print_log(h_actor_fc1)

                W_actor_fc2, b_actor_fc2 = self.make_layer_variables([600, 400], "actor_fc2")
                h_actor_fc2 = tf.nn.relu(tf.matmul(h_actor_fc1, W_actor_fc2) + b_actor_fc2, name="h_actor_fc2")
                self.print_log(h_actor_fc2)

                weight_range = 3 * 10 ** -3
                W_steering, b_steering = self.make_layer_variables([400, 1], "steering", weight_range)
                h_steering = tf.tanh(tf.matmul(h_actor_fc2, W_steering) + b_steering, name="h_steering")
                self.print_log(h_steering)

                W_acc, b_acc = self.make_layer_variables([400, 1], "acc", weight_range)
                h_acc = tf.sigmoid(tf.matmul(h_actor_fc2, W_acc) + b_acc, name="h_acc")
                self.print_log(h_acc)

                if action_group_no == 3:
                    W_brake, b_brake = self.make_layer_variables([400, 1], "brake", weight_range)
                    h_brake = tf.sigmoid(tf.matmul(h_actor_fc2, W_brake) + b_brake, name="h_brake")
                    self.print_log(h_brake)
                    actor_y = tf.concat(1, [h_steering, h_acc, h_brake])
                else:
                    actor_y = tf.concat(1, [h_steering, h_acc])
                self.print_log(actor_y)

            # Critic network
            action_in = tf.placeholder(tf.float32, shape=[None, action_group_no], name="actions")
            with tf.variable_scope('critic'):
                W_critic_fc1, b_critic_fc1 = self.make_layer_variables([input_img_size, 600], "critic_fc1")
                h_critic_fc1 = tf.nn.relu(tf.matmul(x_flat, W_critic_fc1) + b_critic_fc1, name="h_critic_fc1")
                self.print_log(h_critic_fc1)

                h_concat = tf.concat(1, [h_critic_fc1, action_in])

                W_critic_fc2, b_critic_fc2 = self.make_layer_variables([600 + action_group_no, 400], "critic_fc2")
                h_critic_fc2 = tf.nn.relu(tf.matmul(h_concat, W_critic_fc2) + b_critic_fc2, name="h_critic_fc2")
                self.print_log(h_critic_fc2)

                W_critic_fc3, b_critic_fc3 = self.make_layer_variables([400, 1], "critic_fc3", weight_range)
                critic_y = tf.matmul(h_critic_fc2, W_critic_fc3) + b_critic_fc3
                self.print_log(critic_y)

        self.x_in = x_in
        self.action_in = action_in
        self.actor_y = actor_y
        self.critic_y = critic_y

        tvars = tf.trainable_variables()
        self.actor_vars = [tvar for tvar in tvars if tvar.name.startswith(name + '/actor')]
        self.variables = [tvar for tvar in tvars if tvar.name.startswith(name)]
        print 'len(self.actor_vars) : %s' % len(self.actor_vars)
        print 'len(self.variables) : %s' % len(self.variables)


class ModelTorcsPixel(Model):
    def build_network(self, name, network, action_group_no):
        self.print_log('Building network ModelTorcsPixel')

        with tf.variable_scope(name):
            x_in = tf.placeholder(tf.uint8, shape=[None, self.screen_height, self.screen_width, self.history_len], name="screens")
            self.x_normalized = tf.to_float(x_in) / 255.0
            self.print_log(self.x_normalized)

            with tf.variable_scope('actor'):
                W_conv1, b_conv1 = self.make_layer_variables([6, 6, self.history_len, 32], "conv1")
                h_conv1 = tf.nn.relu(tf.nn.conv2d(self.x_normalized, W_conv1, strides=[1, 2, 2, 1], padding='VALID') + b_conv1, name="h_conv1")
                self.print_log(h_conv1)

                W_conv2, b_conv2 = self.make_layer_variables([3, 3, 32, 32], "conv2")
                h_conv2 = tf.nn.relu(tf.nn.conv2d(h_conv1, W_conv2, strides=[1, 2, 2, 1], padding='VALID') + b_conv2, name="h_conv2")
                self.print_log(h_conv2)

                W_conv3, b_conv3 = self.make_layer_variables([3, 3, 32, 32], "conv3")
                h_conv3 = tf.nn.relu(tf.nn.conv2d(h_conv2, W_conv3, strides=[1, 2, 2, 1], padding='VALID') + b_conv3, name="h_conv3")
                self.print_log(h_conv3)

                conv_out_size = np.prod(h_conv3._shape[1:]).value
                h_conv3_flat = tf.reshape(h_conv3, [-1, conv_out_size], name="h_conv3_flat")
                self.print_log(h_conv3_flat)

                W_fc1, b_fc1 = self.make_layer_variables([conv_out_size, 600], "fc1")
                fc1_temp = tf.matmul(h_conv3_flat, W_fc1) + b_fc1
                h_fc1 = tf.nn.relu(fc1_temp, name="h_fc1")
                self.print_log(h_fc1)

                W_fc2, b_fc2 = self.make_layer_variables([600, 400], "fc2")
                h_fc2 = tf.nn.relu(tf.matmul(h_fc1, W_fc2) + b_fc2, name="h_fc2")
                self.print_log(h_fc2)

                # Actor specific network
                weight_range = 3 * 10 ** -4
                W_steering, b_steering = self.make_layer_variables([400, 1], "steering", weight_range)
                h_steering = tf.tanh(tf.matmul(h_fc2, W_steering) + b_steering, name="h_steering")
                self.print_log(h_steering)

                W_acc, b_acc = self.make_layer_variables([400, 1], "acc", weight_range)
                h_acc = tf.sigmoid(tf.matmul(h_fc2, W_acc) + b_acc, name="h_acc")
                self.print_log(h_acc)

                if action_group_no == 3:
                    W_brake, b_brake = self.make_layer_variables([400, 1], "brake", weight_range)
                    h_brake = tf.sigmoid(tf.matmul(h_fc2, W_brake) + b_brake, name="h_brake")
                    self.print_log(h_brake)
                    actor_y = tf.concat(1, [h_steering, h_acc, h_brake])
                else:
                    actor_y = tf.concat(1, [h_steering, h_acc])
                self.print_log(actor_y)

            # Critic specific network
            action_in = tf.placeholder(tf.float32, shape=[None, action_group_no], name="actions")
            with tf.variable_scope('critic'):
                W_critic_fc1, b_critic_fc1 = self.make_layer_variables([action_group_no, 600], "critic_fc1")
                critic_fc1_temp = tf.matmul(action_in, W_critic_fc1) + b_critic_fc1
                self.print_log(critic_fc1_temp)

                self.h_critic_fc1 = tf.nn.relu(fc1_temp + critic_fc1_temp , name="h_critic_fc1")
                self.print_log(self.h_critic_fc1)

                self.h_critic_fc2 = tf.nn.relu(tf.matmul(self.h_critic_fc1, W_fc2) + b_fc2, name="h_critic_fc2")
                self.print_log(self.h_critic_fc2)

                W_critic_fc3, b_critic_fc3 = self.make_layer_variables([400, 1], "critic_fc3", weight_range)
                critic_y = tf.matmul(self.h_critic_fc2, W_critic_fc3) + b_critic_fc3
                self.print_log(critic_y)

        self.x_in = x_in
        self.action_in = action_in
        self.actor_y = actor_y
        self.critic_y = critic_y

        tvars = tf.trainable_variables()
        self.actor_vars = [tvar for tvar in tvars if tvar.name.startswith(name + '/actor')]
        self.variables = [tvar for tvar in tvars if tvar.name.startswith(name)]
        print 'len(self.actor_vars) : %s' % len(self.actor_vars)
        print 'len(self.variables) : %s' % len(self.variables)

