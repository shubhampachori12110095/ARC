import argparse
parser = argparse.ArgumentParser(description="Command Line Interface for Setting HyperParameter Values")
parser.add_argument("-n", "--expt-name", type=str, default="w_o_test", help="experiment name for logging purposes")
parser.add_argument("-l", "--learning-rate", type=float, default=1e-3, help="global leaning rate")
parser.add_argument("-i", "--image-size", type=int, default=32, help="size of the square input image (side)")
parser.add_argument("-b", "--batch-size", type=int, default=32, help="batch size for training")
parser.add_argument("-t", "--testing", action="store_true", help="report test set results")
parser.add_argument("-m", "--max-iter", type=int, default=100000, help="number of iteration to train the net for")
parser.add_argument("-d", "--depth", type=int, default=16, help="the resnet is 3d+2 resnet blocks deep")
parser.add_argument("-k", "--width", type=int, default=4, help="width multiplier for each WRN block")

meta_data = vars(parser.parse_args())

for md in meta_data.keys():
	print md, meta_data[md]

expt_name = meta_data["expt_name"]
learning_rate = meta_data["learning_rate"]
image_size = meta_data["image_size"]
batch_size = meta_data["batch_size"]
N_ITER_MAX = meta_data["max_iter"]
wrn_n = meta_data["depth"]
wrn_k = meta_data["width"]

data_split = [30, 10]
val_freq = 1000
val_batch_size = batch_size * 4
val_num_batches = 200
test_num_batches = 2000

print "... importing libraries"
import sys
sys.setrecursionlimit(10000)

import numpy as np

import theano
import theano.tensor as T

import lasagne
from lasagne.layers import InputLayer
from lasagne.layers import DenseLayer, DropoutLayer
from lasagne.layers import batch_norm, BatchNormLayer, ExpressionLayer
from lasagne.layers import Conv2DLayer as ConvLayer
from lasagne.layers import ElemwiseSumLayer, NonlinearityLayer, GlobalPoolLayer
from lasagne.nonlinearities import rectify, softmax, sigmoid
from lasagne.init import HeNormal
from lasagne.layers import get_all_params, get_all_layers, get_output
from lasagne.regularization import regularize_layer_params
from lasagne.objectives import binary_crossentropy
from lasagne.updates import adam
from lasagne.layers import helper

from data_workers import Omniglot

import time
import cPickle
import gzip


def residual_block(l, increase_dim=False, projection=True, first=False, filters=16):
	if increase_dim:
		first_stride = (2, 2)
	else:
		first_stride = (1, 1)
	if first:
		bn_pre_relu = l
	else:
		bn_pre_conv = BatchNormLayer(l)
		bn_pre_relu = NonlinearityLayer(bn_pre_conv, rectify)
	conv_1 = batch_norm(ConvLayer(bn_pre_relu, num_filters=filters, filter_size=(3,3), stride=first_stride, nonlinearity=rectify, pad='same', W=HeNormal(gain='relu')))
	dropout = DropoutLayer(conv_1, p=0.3)
	conv_2 = ConvLayer(dropout, num_filters=filters, filter_size=(3,3), stride=(1,1), nonlinearity=None, pad='same', W=HeNormal(gain='relu'))
	if increase_dim:
		projection = ConvLayer(l, num_filters=filters, filter_size=(1,1), stride=(2,2), nonlinearity=None, pad='same', b=None)
		block = ElemwiseSumLayer([conv_2, projection])
	elif first:
		projection = ConvLayer(l, num_filters=filters, filter_size=(1,1), stride=(1,1), nonlinearity=None, pad='same', b=None)
		block = ElemwiseSumLayer([conv_2, projection])
	else:
		block = ElemwiseSumLayer([conv_2, l])
	return block

print "... setting up the network"
n_filters = {0: 16, 1: 16 * wrn_k, 2: 32 * wrn_k, 3: 64 * wrn_k}

X = T.tensor4("input")
y = T.imatrix("target")

l_in = InputLayer(shape=(None, 1, image_size, image_size), input_var=X)
l = batch_norm(ConvLayer(l_in, num_filters=n_filters[0], filter_size=(3, 3), \
	stride=(1, 1), nonlinearity=rectify, pad='same', W=HeNormal(gain='relu')))
l = residual_block(l, first=True, filters=n_filters[1])
for _ in range(1, wrn_n):
	l = residual_block(l, filters=n_filters[1])
l = residual_block(l, increase_dim=True, filters=n_filters[2])
for _ in range(1, (wrn_n+2)):
	l = residual_block(l, filters=n_filters[2])
l = residual_block(l, increase_dim=True, filters=n_filters[3])
for _ in range(1, (wrn_n+2)):
	l = residual_block(l, filters=n_filters[3])

bn_post_conv = BatchNormLayer(l)
bn_post_relu = NonlinearityLayer(bn_post_conv, rectify)
avg_pool = GlobalPoolLayer(bn_post_relu)
dense_layer = DenseLayer(avg_pool, num_units=128, W=HeNormal(), nonlinearity=rectify)
dist_layer = ExpressionLayer(dense_layer, lambda I: T.abs_(I[:I.shape[0]/2] - I[I.shape[0]/2:]), output_shape='auto')
output_layer = DenseLayer(dist_layer, num_units=1, nonlinearity=sigmoid)

prediction = get_output(output_layer)
prediction_clean = get_output(output_layer, deterministic=True)

loss = T.mean(binary_crossentropy(prediction, y))
accuracy = T.mean(T.eq(prediction_clean > 0.5, y), dtype=theano.config.floatX)

all_layers = get_all_layers(output_layer)
l2_penalty = 0.0001 * regularize_layer_params(all_layers, lasagne.regularization.l2)
loss = loss + l2_penalty

params = get_all_params(output_layer, trainable=True)
updates = adam(loss, params, learning_rate=learning_rate)

print "... compiling"
train_fn = theano.function(inputs=[X, y], outputs=loss, updates=updates)
val_fn = theano.function(inputs=[X, y], outputs=[loss, accuracy])

print "... loading dataset"
worker = Omniglot(img_size=image_size, data_split=data_split)

print "... begin training"
meta_data["training_loss"] = []
meta_data["validation_loss"] = []
meta_data["validation_accuracy"] = []

best_val_acc = 0.0
iter_n = 0
best_iter_n = 0
best_params = helper.get_all_param_values(output_layer)

smooth_loss = 0.6932
try:
	while iter_n < N_ITER_MAX:
		iter_n += 1

		tick = time.clock()
		X_train, y_train = worker.fetch_verif_batch(batch_size, 'train')
		X_train = X_train.reshape(-1, 1, image_size, image_size)
		batch_loss = train_fn(X_train, y_train)
		tock = time.clock()

		smooth_loss = 0.95 * smooth_loss + 0.05 * batch_loss
		print "iteration: ", iter_n, " | training loss: ", smooth_loss, " | batch run time: ", np.round((tock - tick), 3) * 1000, "ms"
		meta_data["training_loss"].append((iter_n, batch_loss))

		if np.isnan(batch_loss):
			print "****" * 100
			print "NaNs Detected"
			break

		if iter_n % val_freq == 0:
			net_val_loss, net_val_acc = 0.0, 0.0
			for i in range(val_num_batches):
				X_val, y_val = worker.fetch_verif_batch(val_batch_size, 'val')
				X_val = X_val.reshape(-1, 1, image_size, image_size)
				val_loss, val_acc = val_fn(X_val, y_val)
				net_val_loss += val_loss
				net_val_acc += val_acc
			val_loss = net_val_loss / val_num_batches
			val_acc = net_val_acc / val_num_batches

			print "****" * 20
			print "validation loss: ", val_loss
			print "validation accuracy: ", val_acc * 100.0
			print "****" * 20

			meta_data["validation_loss"].append((iter_n, val_loss))
			meta_data["validation_accuracy"].append((iter_n, val_acc))

			if val_acc > best_val_acc:
				best_val_acc = val_acc
				best_iter_n = iter_n
				best_params = helper.get_all_param_values(output_layer)

except KeyboardInterrupt:
	pass

print "... training done"
print "best validation accuracy: ", best_val_acc * 100.0, " at iteration number: ", best_iter_n

if meta_data["testing"]:
	"... setting up testing network"
	helper.set_all_param_values(l_y, best_params)
	net_test_loss, net_test_acc = 0.0, 0.0
	for i in range(test_num_batches):
		X_test, y_test = worker.fetch_verif_batch(batch_size, 'test')
		X_test = X_test.reshape(-1, 1, image_size, image_size)
		test_loss, test_acc = val_fn(X_test, y_test)
		net_test_loss += test_loss
		net_test_acc += test_acc
	test_loss = net_test_loss / test_num_batches
	test_acc = net_test_acc / test_num_batches

	print "====" * 20
	print "final testing loss: ", test_loss
	print "final testing accuracy: ", test_acc * 100.0
	print "====" * 20

	meta_data["testing_loss"] = test_loss
	meta_data["testing_accuracy"] = test_acc

print "... serializing metadata"
log_md = gzip.open("results/" + str(expt_name) + ".mtd", "wb")
cPickle.dump(meta_data, log_md)
log_md.close()

print "... serializing parameters"
log_p = gzip.open("results/" + str(expt_name) + ".params", "wb")
cPickle.dump(best_params, log_p)
log_p.close()

print "... exiting ..."
