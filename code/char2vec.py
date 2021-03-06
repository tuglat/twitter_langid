"""Models to create word embeddings from character sequences.

Use BasicEmbedding for traditional word embeddings or use the CharLSTM or
CharCNN implementations of char2vec.
"""
import numpy as np
import tensorflow as tf
import util

initializer = tf.random_uniform_initializer(-0.1, 0.1)


class BasicEmbedding(object):
  """ Baseline (traditional) word embeddings to compare against c2v.

  If building a model that uses a concatenation of traditional word embeddings
  with those generated by char2vec then it can be useful to use dropout on
  the traditional embeddings to avoid favoring them too much during early
  training.
  """

  def __init__(self, model_params, vocab_size=None,
               dropout_keep_prob=None):
    self.dropout_keep_prob = dropout_keep_prob
    self.embedding_dims = model_params['word_embed_dims']
    self.word_embeddings = tf.get_variable("word_embeddings",
                                           [vocab_size, self.embedding_dims],
                                           initializer=initializer)

  def GetEmbeddings(self, x):
    """Looks up some embeddings from the embedding table.

    Args:
        x: matrix of word ids to look up

    Returns:
        word embedding vectors for the given ids.
    """
    e = tf.nn.embedding_lookup(self.word_embeddings, x)
    if self.dropout_keep_prob:
      e = tf.nn.dropout(e, self.dropout_keep_prob)
    return e

  def SaveVariables(self, *nargs):
    pass


class Char2Vec(object):
  """Maps character sequences to word embeddings."""

  def __init__(self, char_vocab, max_sequence_len=15):
    """Initialize the Char2Vec model.

    Args:
      char_vocab: vocab class instance for the character vocabulary
      max_sequence_len: length of the longest word
    """
    self.max_sequence_len = max_sequence_len
    self.char_vocab = char_vocab
    self._vocab_size = char_vocab.vocab_size

    # Placeholder for the character sequences in the form of an
    # n x k array where n is the number of words and k is the
    # length of the longest word. Characters are encoded as ints.
    self.words_as_chars = tf.placeholder(tf.int32, [None, max_sequence_len],
                                         name='words_as_chars')

  def GetEmbeddings(self, x):
    return tf.nn.embedding_lookup(self.word_embeddings, x)

  def MakeMat(self, word_list, pad_len=None):
    """Make a matrix to hold the character sequences in.

    Special start and end tokens are added to the beggining and end of
    each word.

    Args:
      word_list: A list of strings
      pad_len: Pad all character sequences to this length. If a word is
               longer than the pad_len it will be truncated.

    Returns:
      Array containing character sequences and a vector of sequence lengths.
    """
    if not pad_len:
      pad_len = self.max_sequence_len

    # make the padded char mat
    the_words = []
    word_lengths = []
    for word in word_list:
      word_idx = [self.char_vocab[c] for c in util.Graphemes(word)]
      word_idx = ([self.char_vocab['<S>']] + word_idx[:pad_len-2] +
                  [self.char_vocab['</S>']])
      if len(word_idx) < pad_len:
        word_idx += [self.char_vocab['</S>']] * (pad_len - len(word_idx))
      the_words.append(word_idx)
      word_lengths.append(min(pad_len, len(word)+2))

    the_words = np.array(the_words)
    word_lengths = np.array(word_lengths)
    return the_words, word_lengths

  @staticmethod
  def GetBatchVocab(words):
    batch_vocab = np.unique(words)
    words_remapped = np.copy(words)
    for i in xrange(len(batch_vocab)):
      np.place(words_remapped, words==batch_vocab[i], i)
    return batch_vocab, words_remapped


class CharLSTM(Char2Vec):
  """ LSTM implementation of char2vec.

  The model is a two layer deep bi-LSTM. Dropout is used
  as a regularizer on the 2nd layer. The output word embeddings
  are available in the c2v.word_embeddings variable.
  
  The required placeholders are words_as_chars and seq_lens, which
  contain the words in the form of padded character sequences and
  the length of each word respectively. The batch_dim placeholder
  must be supplied.
  """

  def __init__(self, char_vocab, model_params,
               max_sequence_len=15, dropout_keep_prob=None):
    super(CharLSTM, self).__init__(char_vocab, max_sequence_len)

    char_embed_dims = np.log(len(char_vocab)) + 1
    word_embed_dims = model_params['word_embed_dims']
    self.embedding_dims = word_embed_dims

    layer1_hidden_size = model_params['c2v_layer1_hidden_size']
    layer1_out_size = model_params['c2v_layer1_out_size']
    self.hidden_size = model_params['c2v_layer2_hidden_size']

    # Placeholder for the word length vector.
    self.seq_lens = tf.placeholder(tf.int64, [None], name='seq_lens')

    # Placeholder for the number of words (n) in the minibatch.
    self.batch_dim = tf.placeholder(tf.int32, name='char_batch_dim')

        # The following variables define the model.
    with tf.variable_scope('c2v'):

      # continuous space character embeddings
      self.embedding = tf.get_variable("embedding",
                                       [self._vocab_size, char_embed_dims],
                                       initializer=initializer)

      def GetCell(hidden_size, num_proj=None, use_peepholes=False):
        """Helper function to make LSTM cells."""
        layer = LSTMCell(hidden_size, num_proj=num_proj,
                         use_peepholes=use_peepholes)
        layer = tf.nn.rnn.rnn_cell.DropoutWrapper(layer,
          output_keep_prob=dropout_keep_prob,
          input_keep_prob=dropout_keep_prob)
        return layer

      # This is the 1st bi-LSTM layer.
      layer1_fw = GetCell(layer1_hidden_size, layer1_out_size, model_params['peepholes'])
      layer1_bw = GetCell(layer1_hidden_size, layer1_out_size, model_params['peepholes'])

      # This is the 2nd layer, also a bi-LSTM. The input size is twice
      # the size of the output size from layer one because the concatenation
      # of the layer one outputs is the layer 2 input.
      if layer1_out_size:  # Check if proj layer is enabled
        layer2_input_size = 2 * layer1_out_size
      else:
        layer2_input_size = 2 * layer1_hidden_size

      layer2_fw = LSTMCell(self.hidden_size, use_peepholes=model_params['peepholes'])
      layer2_bw = LSTMCell(self.hidden_size, use_peepholes=model_params['peepholes'])

      # The final embeddings is the output from the layer2 LSTM multiplied
      # by this matrix. One purpose of this matrix is to scale the layer2
      # output to match the specified number of dimentions for the word
      # embedding.
      out_mat = tf.get_variable('out_mat',
                                [2 * self.hidden_size, word_embed_dims],
                                initializer=initializer)

      # z should be a tensor of dimensions batch_sz x word_len x embed_dims.
      z = tf.nn.embedding_lookup(self.embedding, self.words_as_chars)

      # Each entry in this list is a matrix of dim batch_sz x embed_dims.
      # There is one entry per timestep and one character is processed per
      # timestep.
      inputs = [tf.squeeze(input_) for input_ in
                tf.split(1, max_sequence_len, z)]
      for _i in inputs:  # newest version of tf needs help with shape inference
        _i.set_shape((None, char_embed_dims))

      # Feed the inputs through a bidirectional LSTM. Output is a list of
      # word_len tensors with dim batch_sz x 2 * hidden_sz.
      out1, _, _ = tf.nn.rnn.bidirectional_rnn(layer1_fw, layer1_bw, inputs,
                                               dtype=tf.float32,
                                               sequence_length=self.seq_lens)

      # For the 2nd bi-LSTM layer we won't use the bidirectional_rnn
      # wrapper. This is because we only want to save the last output
      # from the forward direction and the first output from the backward
      # direction. It is a little tricky to grab these states because they
      # are in different positions for each word in the minibatch.
      batch_range = tf.range(self.batch_dim)
      # The slices variables keeps track of which position the appropriate
      # outputs are for each word in the minibatch.
      slices = self.batch_dim * tf.to_int32(self.seq_lens-1) + batch_range
      with tf.variable_scope('fw'):
        outputs_forward, _ = tf.nn.rnn.rnn(layer2_fw, out1, dtype=tf.float32)
        out_forward = self._GetLastOutput(outputs_forward, slices)
      with tf.variable_scope('bw'):
        # Reverse the sequences before processing with the backwards LSTM.
        out1_bw = reverse_seq(out1, self.seq_lens)
        outputs_backward, _ = tf.nn.rnn.rnn(layer2_bw, out1_bw, dtype=tf.float32)
        out_backward = self._GetLastOutput(outputs_backward, slices)

    # This is the concatenation of the output from the two directions.
    out = tf.concat(1, [out_forward, out_backward])

    # Project to the proper output dimension. This is a dimensionality
    # reduction step.
    self.word_embeddings = tf.matmul(out, out_mat)

  def _GetLastOutput(self, outputs, slices):
    """Helper function to pull out the last output for each word."""
    reshaped = tf.reshape(tf.pack(outputs), [-1, self.hidden_size])
    return tf.gather(reshaped, slices)


class CharCNN(Char2Vec):
  """CNN implementation of char2vec.

  The model uses two layers of convolution. The second one is followed by a
  max pooling operation. After that there is a resnet layer.
  """

  def __init__(self, char_vocab, model_params,
               max_sequence_len=15, dropout_keep_prob=None):
    super(CharCNN, self).__init__(char_vocab, max_sequence_len)

    char_embed_dims = int(np.log(len(char_vocab))) + 1

    layer1_out_size = model_params['c2v_layer1_out_size']
    hidden_size = model_params['c2v_layer2_hidden_size']
    word_embed_dims = model_params['word_embed_dims']

    # The following variables define the model.
    with tf.variable_scope('c2v'):
      # continuous space character embeddings
      self.embedding = tf.get_variable("embedding",
                                       [self._vocab_size, char_embed_dims],
                                       initializer=initializer)
      the_filter, filter_b = MakeFilter(3, char_embed_dims, layer1_out_size,
                                        'filt')

      # z is a tensor of dimensions batch_sz x word_len x embed_dims.
      z = tf.nn.embedding_lookup(self.embedding, self.words_as_chars)
      z_expanded = tf.expand_dims(z, -1)

      conv = tf.nn.conv2d(z_expanded, the_filter, strides=[1, 1, 1, 1],
                          padding='VALID' )
      h = tf.nn.relu(tf.nn.bias_add(tf.squeeze(conv), filter_b))
      h.set_shape((None, max_sequence_len - 2, layer1_out_size))
      if dropout_keep_prob is not None:
        h_expanded = tf.nn.dropout(tf.expand_dims(h, -1), dropout_keep_prob)
      else:
        h_expanded = tf.expand_dims(h, -1)

      pools = []
      filter_sizes = range(3,6)
      for width in filter_sizes:
        f, f_bias = MakeFilter(width, layer1_out_size, hidden_size,
                               'filter_w{0}'.format(width))
        conv2 = tf.nn.conv2d(h_expanded, f, strides=[1, 1, 1, 1],
                             padding='VALID')
        h2 = tf.nn.relu(tf.nn.bias_add(conv2, f_bias))
        pooled = tf.nn.max_pool(h2, ksize=[1, max_sequence_len-1-width, 1, 1],
                                strides=[1, 1, 1, 1], padding='VALID')
        pools.append(pooled)

        if width == 3:  # debugging
          self.hh = tf.squeeze(pooled)
          self.hidx = tf.argmax(h2, 1)

      pooled = tf.squeeze(tf.concat(3, pools), [1,2])

      # resnet layer https://arxiv.org/abs/1512.03385
      sz = len(filter_sizes) * hidden_size
      t_mat = tf.get_variable('t_mat', [sz, sz])
      t_bias = tf.Variable(tf.constant(0.1, shape=[sz]), name='t_bias')
      t = tf.nn.relu(tf.matmul(pooled, t_mat) + t_bias)

      self.word_embeddings = t + pooled
      self.embedding_dims = sz


def MakeFilter(width, in_size, num_filters, name):
  filter_sz = [width, in_size, 1, num_filters]
  filter_b = tf.Variable(tf.constant(0.1, shape=[num_filters]),
                         name='{0}_bias'.format(name))
  the_filter = tf.get_variable(name, filter_sz)
  return the_filter, filter_b


def reverse_seq(input_seq, lengths):
  """Reverse a list of Tensors up to specified lengths.
  Args:
    input_seq: Sequence of seq_len tensors of dimension (batch_size, depth)
    lengths:   A tensor of dimension batch_size, containing lengths for each
               sequence in the batch. If "None" is specified, simply reverses
               the list.
  Returns:
    time-reversed sequence
  """
  for input_ in input_seq:
    input_.set_shape(input_.get_shape().with_rank(2))

  # Join into (time, batch_size, depth)
  s_joined = tf.pack(input_seq)

  # Reverse along dimension 0
  s_reversed = tf.reverse_sequence(s_joined, lengths, 0, 1)
  # Split again into list
  result = tf.unpack(s_reversed)
  return result
