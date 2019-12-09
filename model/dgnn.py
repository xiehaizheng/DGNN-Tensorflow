from graph.directed_ntu_rgb_d import Graph
import tensorflow as tf

"""scale_gradients: helper function for AdaptiveIncidence
"""
@tf.custom_gradient
def scale_gradients(incidence_matrix, incidence_lambda):
    def custom_grad(dy):
        return tf.math.multiply(dy, incidence_lambda)
    return incidence_matrix, custom_grad

"""AdaptiveIncidence: stores incidence matrix as trainable Variable
                      and allows us to control the scale of its gradient
call Args:
    inputs: tensor, output of preceding layer
    incidence_lambda: double, specifying scaling factor of gradient
Returns:
    A Keras layer instance.
"""
class AdaptiveIncidence(tf.keras.layers.Layer):
  def __init__(self, incidence_matrix):
    super().__init__()
    self.incidence_matrix = tf.Variable(initial_value=incidence_matrix,
                                        trainable=True,
                                        dtype=tf.float32)

  def call(self, incidence_lambda):
    return scale_gradients(self.incidence_matrix, incidence_lambda)

'''
DGN Block
Args:
  filters     : number of hidden units for h^v, h^e update functions
  source_A    : Incidence matrix of source vertexes
  target_A    : Incidence matrix of target vertexes
  activation  : activation function to use in all layers in the block
Returns:
  A Keras model instance for the block.
'''
class DGNBlock(tf.keras.Model):
    def __init__(self, filters, source_A, target_A, activation='relu'):
        super().__init__()

        self.num_nodes = tf.shape(source_A)[0]
        self.num_edges = tf.shape(source_A)[1]

        # Adaptive block with learnable graphs; shapes (V_node, V_edge)
        self.source_A = AdaptiveIncidence(source_A)
        self.target_A = AdaptiveIncidence(target_A)

        # Updating functions
        self.H_v = tf.keras.layers.Dense(filters, activation=None)
        self.H_e = tf.keras.layers.Dense(filters, activation=None)

        self.bn_v = tf.keras.layers.BatchNormalization(axis=-1)
        self.bn_e = tf.keras.layers.BatchNormalization(axis=-1)

        self.act = tf.keras.layers.Activation(activation)

    '''
    forward propagation function
    Notes:
      C: number of channels
      T: number of frames/timesteps
      Nv: number of vertexes
      Ne: number of edges
    Args:
      fv:  [BatchSize x T x Nv x C], vertexe data;
      fe:  [BatchSize x T x Ne x C], edge data
      training: bool, True if model is training, else false
      incidence_lambda : float, controls the gradient wrt incidence matrices,
                         value of 0 will disable learning of the matrix,
                         value of 1 will allow normal gradient flow thereby
                         allowing learning of the matrix. Can also be anything
                         inbetween to change amount of learning of the matrix
    Returns:
      forward propagation result
    '''
    def call(self, fv, fe, training, incidence_lambda):
        BatchSize = tf.shape(fv)[0]
        T = tf.shape(fv)[1]
        Nv = tf.shape(fv)[2]
        Ne = tf.shape(fe)[2]
        C = tf.shape(fv)[3]

        # permute to (BatchSize, C, T, N_v/N_e)
        fv = tf.transpose(fv, perm=[0,3,2,1])
        fe = tf.transpose(fe, perm=[0,3,2,1])

        # Reshape for matmul, shape: (BatchSize, CT, N_v/N_e)
        fv = tf.reshape(fv, (BatchSize, -1, Nv))
        fe = tf.reshape(fe, (BatchSize, -1, Ne))

        # Compute features for node/edge updates
        feAs = tf.einsum('nce,ev->ncv', fe, tf.transpose(self.source_A(incidence_lambda)))
        feAt = tf.einsum('nce,ev->ncv', fe, tf.transpose(self.target_A(incidence_lambda)))
        fv_inp = tf.stack([fv, feAs, feAt], axis=1) # Out shape: (BatchSize,3,CT,Nv)
        fv_inp = tf.transpose(tf.reshape(fv_inp, (BatchSize, 3 * C, T, Nv)), perm=[0,2,3,1]) # Out shape: (BatchSize,T,Nv,3C)

        fv_out = self.H_v(fv_inp) # Out shape: (BatchSize,T,Nv,C_out)
        fv_out = self.bn_v(fv_out, training=training)
        fv_out = self.act(fv_out)

        fvAs = tf.einsum('nce,ev->ncv', fv, tf.transpose(self.source_A(incidence_lambda)))
        fvAt = tf.einsum('nce,ev->ncv', fv, tf.transpose(self.target_A(incidence_lambda)))
        fe_inp = tf.stack([fe, fvAs, fvAt], axis=1) # Out shape: (BatchSize,3,CT,Ne)
        fe_inp = tf.transpose(tf.reshape(fe_inp, (BatchSize, 3 * C, T, Ne)), perm=[0,2,3,1]) # Out shape: (BatchSize,T,Ne,3C)

        fe_out = self.H_e(fe_inp) # Out shape: (BatchSize,T,Ne,C_out)
        fe_out = self.bn_e(fe_out, training=training)
        fe_out = self.act(fe_out)

        return fv_out, fe_out

class TemporalConv(tf.keras.Model):
    def __init__(self, filters, kernel_size=9, stride=1, activation='relu'):
        super().__init__()

        self.conv = tf.keras.layers.Conv2D(filters, [kernel_size, 1], [stride, 1],
                                           padding='same',
                                           kernel_initializer='he_normal')
        self.bn   = tf.keras.layers.BatchNormalization(axis=-1)
        self.act  = tf.keras.layers.Activation(activation)

    # Input shape:  (BatchSize,T,Nv,C)
    # Output shape: (BatchSize,T,Nv,filters)
    def call(self, x, training):
        x = self.conv(x)
        x = self.bn(x, training=training)
        x = self.act(x)
        return x

class GraphTemporalConv(tf.keras.Model):
    def __init__(self, filters, source_A, target_A, kernel_size=9, stride=1, activation='relu', residual=True):
        super().__init__()
        self.dgnb = DGNBlock(filters, source_A, target_A, activation)
        self.tc = TemporalConv(filters, kernel_size, stride, activation)

    def call(self, fv, fe, training, incidence_lambda):
        fv, fe = self.dgnb(fv, fe, training=training, incidence_lambda=incidence_lambda)
        fv = self.tc(fv, training=training)
        fe = self.tc(fe, training=training)

        return fv, fe

class DGNN(tf.keras.Model):
    def __init__(self, num_classes=60):
        super().__init__()

        self.graph = Graph()
        source_A = tf.convert_to_tensor(self.graph.source_M, dtype=tf.float32)
        target_A = tf.convert_to_tensor(self.graph.target_M, dtype=tf.float32)

        self.bn_v = tf.keras.layers.BatchNormalization(axis=1)
        self.bn_e = tf.keras.layers.BatchNormalization(axis=1)

        self.GTC_layers = []
        self.GTC_layers.append(GraphTemporalConv(64,  source_A, target_A))
        self.GTC_layers.append(GraphTemporalConv(64,  source_A, target_A))
        self.GTC_layers.append(GraphTemporalConv(64,  source_A, target_A))
        self.GTC_layers.append(GraphTemporalConv(64,  source_A, target_A))
        self.GTC_layers.append(GraphTemporalConv(128, source_A, target_A, stride=2))
        self.GTC_layers.append(GraphTemporalConv(128, source_A, target_A))
        self.GTC_layers.append(GraphTemporalConv(128, source_A, target_A))
        self.GTC_layers.append(GraphTemporalConv(256, source_A, target_A, stride=2))
        self.GTC_layers.append(GraphTemporalConv(256, source_A, target_A))
        self.GTC_layers.append(GraphTemporalConv(256, source_A, target_A))

        self.fc  = tf.keras.layers.Dense(num_classes, activation=None)
        self.gap = tf.keras.layers.GlobalAveragePooling2D()

    '''
    forward propagation function
    Notes:
      C : number of channels
      T : number of frames/timesteps
      Nv: number of vertexes
      Ne: number of edges
      M : number of people
    Args:
      fv:  [BatchSize x M x T x Nv x C], vertexe data;
      fe:  [BatchSize x M x T x Ne x C], edge data
      training: bool, True if model is training, else false
    Returns:
      forward propagation result
    '''
    def call(self, fv, fe, training, incidence_lambda=0):

        BatchSize = tf.shape(fv)[0]
        M = tf.shape(fv)[1]
        T = tf.shape(fv)[2]
        Nv = tf.shape(fv)[3]
        Ne = tf.shape(fe)[3]
        C = tf.shape(fv)[4]

        #merge M axis into BatchSize axis
        fv = tf.reshape(fv, [-1, T, Nv, C])
        fe = tf.reshape(fe, [-1, T, Ne, C])

        # Apply batch norm on time/frame axis
        fv = self.bn_v(fv, training=training)
        fe = self.bn_v(fe, training=training)

        for layer in self.GTC_layers:
            fv, fe = layer(fv, fe, training=training, incidence_lambda=incidence_lambda)

        # Shape: (BatchSize*M,T,V,C), C is same for fv/fe
        out_channels = tf.shape(fv)[3]

        #unmerge M axis from BatchSize axis
        fv = tf.reshape(fv, [BatchSize, M, -1, out_channels])
        fe = tf.reshape(fe, [BatchSize, M, -1, out_channels])

        # Performs pooling over both nodes and frames, and over number of persons
        fv = self.gap(fv)
        fe = self.gap(fe)

        # Concat node and edge features
        out = tf.concat([fv, fe], axis=-1)
        out = self.fc(out)
        return out

if __name__ == "__main__":
    pass
