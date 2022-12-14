lenet_model = tf.keras.Sequential([
    InputLayer(input_shape = (IM_SIZE, IM_SIZE, 3)),

    Conv2D(filters = N_FILTERS , kernel_size = KERNEL_SIZE, strides = N_STRIDES , padding='valid',
          activation = 'relu',kernel_regularizer = L2(REGULARIZATION_RATE)),
    BatchNormalization(),
    MaxPool2D (pool_size = POOL_SIZE, strides= N_STRIDES*2),
    Dropout(rate = DROPOUT_RATE ),

    Conv2D(filters = N_FILTERS*2 + 4, kernel_size = KERNEL_SIZE, strides=N_STRIDES, padding='valid',
          activation = 'relu', kernel_regularizer = L2(REGULARIZATION_RATE)),
    BatchNormalization(),
    MaxPool2D (pool_size = POOL_SIZE, strides= N_STRIDES*2),

    Flatten(),
    
    Dense( CONFIGURATION['N_DENSE_1'], activation = "relu", kernel_regularizer = L2(REGULARIZATION_RATE)),
    BatchNormalization(),
    Dropout(rate = DROPOUT_RATE),
    
    Dense( CONFIGURATION['N_DENSE_2'], activation = "relu", kernel_regularizer = L2(REGULARIZATION_RATE)),
    BatchNormalization(),

    Dense(1, activation = "sigmoid"),

])

"""Unet model"""

# standard library

# internal
from .base_model import BaseModel
from dataloader.dataloader import DataLoader

# external
import tensorflow as tf
from tensorflow_examples.models.pix2pix import pix2pix


class UNet(BaseModel):
    """Unet Model Class"""
    def __init__(self, config):
        super().__init__(config)
        self.base_model = tf.keras.applications.MobileNetV2(input_shape=self.config.model.input, include_top=False)
        self.model = None
        self.output_channels = self.config.model.output

        self.dataset = None
        self.info = None
        self.batch_size = self.config.train.batch_size
        self.buffer_size = self.config.train.buffer_size
        self.epoches = self.config.train.epoches
        self.val_subsplits = self.config.train.val_subsplits
        self.validation_steps = 0
        self.train_length = 0
        self.steps_per_epoch = 0

        self.image_size = self.config.data.image_size
        self.train_dataset = []
        self.test_dataset = []

    def load_data(self):
        """Loads and Preprocess data """
        self.dataset, self.info = DataLoader().load_data(self.config.data)
        self._preprocess_data()

    def _preprocess_data(self):
        """ Splits into training and test and set training parameters"""
        train = self.dataset['train'].map(self._load_image_train, num_parallel_calls=tf.data.experimental.AUTOTUNE)
        test = self.dataset['test'].map(self._load_image_test)

        self.train_dataset = train.cache().shuffle(self.buffer_size).batch(self.batch_size).repeat()
        self.train_dataset = self.train_dataset.prefetch(buffer_size=tf.data.experimental.AUTOTUNE)
        self.test_dataset = test.batch(self.batch_size)

        self._set_training_parameters()

    def _set_training_parameters(self):
        """Sets training parameters"""
        self.train_length = self.info.splits['train'].num_examples
        self.steps_per_epoch = self.train_length // self.batch_size
        self.validation_steps = self.info.splits['test'].num_examples // self.batch_size // self.val_subsplits

    def _normalize(self, input_image, input_mask):
        """ Normalise input image
        Args:
            input_image (tf.image): The input image
            input_mask (int): The image mask
        Returns:
            input_image (tf.image): The normalized input image
            input_mask (int): The new image mask
        """
        input_image = tf.cast(input_image, tf.float32) / 255.0
        input_mask -= 1
        return input_image, input_mask

    @tf.function
    def _load_image_train(self, datapoint):
        """ Loads and preprocess  a single training image """
        input_image = tf.image.resize(datapoint['image'], (self.image_size, self.image_size))
        input_mask = tf.image.resize(datapoint['segmentation_mask'], (self.image_size, self.image_size))

        if tf.random.uniform(()) > 0.5:
            input_image = tf.image.flip_left_right(input_image)
            input_mask = tf.image.flip_left_right(input_mask)

        input_image, input_mask = self._normalize(input_image, input_mask)

        return input_image, input_mask

    def _load_image_test(self, datapoint):
        """ Loads and preprocess a single test images"""

        input_image = tf.image.resize(datapoint['image'], (self.image_size, self.image_size))
        input_mask = tf.image.resize(datapoint['segmentation_mask'], (self.image_size, self.image_size))

        input_image, input_mask = self._normalize(input_image, input_mask)

        return input_image, input_mask

    def build(self):
        """ Builds the Keras model based """
        layer_names = [
            'block_1_expand_relu',  # 64x64
            'block_3_expand_relu',  # 32x32
            'block_6_expand_relu',  # 16x16
            'block_13_expand_relu',  # 8x8
            'block_16_project',  # 4x4
        ]
        layers = [self.base_model.get_layer(name).output for name in layer_names]

        # Create the feature extraction model
        down_stack = tf.keras.Model(inputs=self.base_model.input, outputs=layers)

        down_stack.trainable = False

        up_stack = [
            pix2pix.upsample(self.config.model.up_stack.layer_1, self.config.model.up_stack.kernels),  # 4x4 -> 8x8
            pix2pix.upsample(self.config.model.up_stack.layer_2, self.config.model.up_stack.kernels),  # 8x8 -> 16x16
            pix2pix.upsample(self.config.model.up_stack.layer_3, self.config.model.up_stack.kernels),  # 16x16 -> 32x32
            pix2pix.upsample(self.config.model.up_stack.layer_4, self.config.model.up_stack.kernels),  # 32x32 -> 64x64
        ]

        inputs = tf.keras.layers.Input(shape=self.config.model.input)
        x = inputs

        # Downsampling through the model
        skips = down_stack(x)
        x = skips[-1]
        skips = reversed(skips[:-1])

        # Upsampling and establishing the skip connections
        for up, skip in zip(up_stack, skips):
            x = up(x)
            concat = tf.keras.layers.Concatenate()
            x = concat([x, skip])

        # This is the last layer of the model
        last = tf.keras.layers.Conv2DTranspose(
            self.output_channels, self.config.model.up_stack.kernels, strides=2,
            padding='same')  # 64x64 -> 128x128

        x = last(x)

        self.model = tf.keras.Model(inputs=inputs, outputs=x)

    def train(self):
        """Compiles and trains the model"""
        self.model.compile(optimizer=self.config.train.optimizer.type,
                           loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
                           metrics=self.config.train.metrics)

        model_history = self.model.fit(self.train_dataset, epochs=self.epoches,
                                       steps_per_epoch=self.steps_per_epoch,
                                       validation_steps=self.validation_steps,
                                       validation_data=self.test_dataset)

        return model_history.history['loss'], model_history.history['val_loss']

    def evaluate(self):
        """Predicts resuts for the test dataset"""
        predictions = []
        for image, mask in self.dataset.take(1):
            predictions.append(self.model.predict(image))

        return predictions


    def distributed_train(self):
        mirrored_strategy = tf.distribute.MirroredStrategy(devices=["/gpu:0", "/gpu:1"])
        with mirrored_strategy.scope():
            self.model = tf.keras.Model(inputs=inputs, outputs=x)
            self.model.compile(...)
            self.model.fit(...)


        os.environ["TF_CONFIG"] = json.dumps(
            {
                "cluster":{
                    "worker": ["host1:port", "host2:port", "host3:port"]
                },
                "task":{
                     "type": "worker",
                     "index": 1
                }
            }
        )

        multi_worker_mirrored_strategy = tf.distribute.experimental.MultiWorkerMirroredStrategy()
        with multi_worker_mirrored_strategy.scope():
            self.model = tf.keras.Model(inputs=inputs, outputs=x)
            self.model.compile(...)
            self.model.fit(...)

        parameter_server_strategy = tf.distribute.experimental.ParameterServerStrategy()

        os.environ["TF_CONFIG"] = json.dumps(
            {
                "cluster": {
                    "worker": ["host1:port", "host2:port", "host3:port"],
                    "ps":  ["host4:port", "host5:port"]
                },
                "task": {
                    "type": "worker",
                    "index": 1
                }
            }
            
'''
feature_extractor_model = Model(func_input, output, name = "Feature_Extractor")
feature_extractor_model.summary()
feature_extractor_seq_model = tf.keras.Sequential([
                             InputLayer(input_shape = (IM_SIZE, IM_SIZE, 3)),

                             Conv2D(filters = 6, kernel_size = 3, strides=1, padding='valid', activation = 'relu'),
                             BatchNormalization(),
                             MaxPool2D (pool_size = 2, strides= 2),

                             Conv2D(filters = 16, kernel_size = 3, strides=1, padding='valid', activation = 'relu'),
                             BatchNormalization(),
                             MaxPool2D (pool_size = 2, strides= 2),

                             

])
feature_extractor_seq_model.summary()

func_input = Input(shape = (IM_SIZE, IM_SIZE, 3), name = "Input Image")

x = feature_extractor_seq_model(func_input)

x = Flatten()(x)

x = Dense(100, activation = "relu")(x)
x = BatchNormalization()(x)

x = Dense(10, activation = "relu")(x)
x = BatchNormalization()(x)

func_output = Dense(1, activation = "sigmoid")(x)

lenet_model_func = Model(func_input, func_output, name = "Lenet_Model")
lenet_model_func.summary()


class BaseModel(ABC):
    '''Abstract Model class that is inherited to all models
    Behaviours:
    Get X and y
    Resize, load as array
    Preprocess input
    Get_config: hyperparameter tuning
    Model: baseline, VGG16, VGG19, ResNet, Inception, Xception
    Unfreeze layer
    Compile
    Fit
    Evaluate: accuracy, losses, classification report, confusion matrix
    Predict with different thresholds (0.5, mean, median)
    Save model
    Df to csv
    '''
    def __init__(self, cfg):
        self.config = config.wandb.config

    @abstractmethod
    def load_data(self):
        pass

    @abstractmethod
    def build(self):
        """model.compile
        """
        pass

    @abstractmethod
    def train(self):
        """model.fit
        """
        pass

    @abstractmethod
    def evaluate(self):
        pass
    
    @abstractmethod
    def predict(self):
        pass

class BaselineModel(BaseModel):
    def __init__(self, config):
       super().__init__(config)
       self.base_model = tf.keras.applications.MobileNetV2(input_shape=self.config.model.input, include_top=False)

    def load_data(self):
        # self.X = 
        # self. y =
        # self.dataset, self.info = DataLoader().load_data(self.config.data )
        # self._preprocess_data()

    def build(self):
        """Builds the Keras model"""

        self.model = tf.keras.Model(inputs=inputs, outputs=x)
        
        layer_names = [
            "base_layer",
            "flatten_layer",
            "dense_layer",
            "prediction_layer",
        ]
        layers = [self.base_model.get_layer(name).output for name in layer_names]
        
        

    def train(self):
        self.model.compile(optimizer=self.config.train.optimizer.type,
                           loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
                           metrics=self.config.train.metrics)

        model_history = self.model.fit(self.train_dataset, epochs=self.epoches,
                                       steps_per_epoch=self.steps_per_epoch,
                                       validation_steps=self.validation_steps,
                                       validation_data=self.test_dataset)

        return model_history.history['loss'], model_history.history['val_loss']

    def evaluate(self):
        predictions = []
        for image, mask in self.dataset.take(1):
            predictions.append(self.model.predict(image))

        return predictions

class VGG16(BaseModel):
  def __init__(self):
    super(LenetModel, self).__init__()

    self.feature_extractor = FeatureExtractor(8, 3, 1, "valid", "relu", 2)

    self.flatten = Flatten()

    self.dense_1 = Dense(100, activation = "relu")
    self.batch_1 = BatchNormalization()

    self.dense_2 = Dense(10, activation = "relu")
    self.batch_2 = BatchNormalization()

    self.dense_3 = Dense(1, activation = "sigmoid")
    
  def call(self, x, training):

    x = self.feature_extractor(x)
    x = self.flatten(x)
    x = self.dense_1(x)
    x = self.batch_1(x)
    x = self.dense_2(x)
    x = self.batch_2(x)
    x = self.dense_3(x)

    return x
    
lenet_sub_classed = LenetModel()
lenet_sub_classed(tf.zeros([1,224,224,3]))
lenet_sub_classed.summary()

'''

"""
class TransferModel:
    """Instantiate a parent class for all transfer learning models"""

    def __init__(self, config):
        self.config = config.wandb.config

    @staticmethod
    def preprocess(x):
        """Preprocess input for the relevant pretrained model
        Args:
        x
        
        Returns:
        x after preprocessing
        """
        x = preproc_vgg16(x)
        
        return x
    
    def load_model(self):
        
        self.model = model
        
        return model

    def set_nontrainable_layers(model):
        
        # Set the first layers to be untrainable
        model.trainable = False
            
        return model
    
    def add_last_layers(model):
        '''Take a pre-trained model, set its parameters as non-trainable, and add additional trainable layers on top'''
        base_model = set_nontrainable_layers(model)
        flatten_layer = layers.Flatten()
        dense_layer = layers.Dense(500, activation='relu')
        prediction_layer = layers.Dense(10, activation='sigmoid')
        
        model = models.Sequential([
            base_model,
            flatten_layer,
            dense_layer,
            prediction_layer
        ])

        return model
    
    def build_model():

        model = load_model()
        model = add_last_layers(model)
        
        model.compile(loss='binary_crossentropy',
                    optimizer=optimizers.Adam(learning_rate=1e-4),
                    metrics=['accuracy'])

        return model

class VGG16(TransferModel):
    def __init__(self, config):
        super().__init__(config)
    
    def load_model():
        
        model = VGG16(weights="imagenet", include_top=False, input_shape=(config.wandb.config['IM_SIZE'], config.wandb.config['IM_SIZE'], 3))
        
        return model

"""

# class BaseModel(ABC):
#     '''Abstract Model class that is inherited to all models
#     Behaviours:
#     Get X and y
#     Resize, load as array
#     Preprocess input
#     Get_config: hyperparameter tuning
#     Model: baseline, VGG16, VGG19, ResNet, Inception, Xception
#     Unfreeze layer
#     Compile
#     Fit
#     Evaluate: accuracy, losses, classification report, confusion matrix
#     Predict with different thresholds (0.5, mean, median)
#     Save model
#     Df to csv
#     '''
#     def __init__(self, config):
#         self.config = config.wandb.config

#     @abstractmethod
#     def load_data(self):
#          pass

#     @abstractmethod
#     def build(self):
#         """model.compile
#         """
#         pass

# class BaselineModel(BaseModel):
#     def __init__(self, config):
#         super().__init__(config)
    
#     def build(self):
#         self.model = load_baseline_model()
#         return self.model
    
# class VGG16Model(BaseModel):
#     def __init__(self, config):
#         super().__init__(config)
    
#     def build(self):
#         model = load_model()
#         model = add_last_layers(model)
        
#         model.compile(loss='binary_crossentropy',
#                     optimizer=optimizers.Adam(learning_rate=1e-4),
#                     metrics=['accuracy'])
#         return model