"""  Runs a previously trained CNN on a new data volume.

  *** KEY ASSUMPTIONS ***
  o Assumes the network contains a layer named "prob" that contains the
    output probabilities for each class.
  o If you are extracting features, assumes feature are associated with
    an inner product layer called "ip1"

  Note that we require the solver file (vs. just the network file)
  since that's where the CPU/GPU flag lives.  Alternatively, we could
  add a cpu/gpu flag to this script and only require the caller
  specify the network file (which would be more consistent with
  Caffe's command line API)

  Note that using a "brute force" approach of extracting every possible subtile from
  an image an then processing that tile using a CNN is fairly inefficient.  In particular,
  adjacent subtiles contain a substantial amount of overlap; e.g. the tiles centered
  on pixels x_{i,j} and x_{i,j+1} have all but the first and last column in common.
  For just the first CNN layer, most of the work in correlating the filters with the
  tile centered on x_{i,j} is repeated in x_{i,j+1}.  A dynamic programming approach
  could greatly help reduce this; e.g. see also [4].

 References:
   [1] Jia, Y. et. al. "Caffe: Convolutional Architecture for Fast Feature Embedding
       arXiv preprint, 2014.

   [2] Ciresan, D., et al. "Deep neural networks segment neuronal membranes in
       electron microscopy images." NIPS 2012.

   [3] https://groups.google.com/forum/#!topic/caffe-users/NKsSbZ3boGg

   [4] Giusti et. al. "Fast Image Scanning with Deep Max-Pooling Convolutional
       Neural Networks," 2013
"""

__author__ = "Mike Pekala"
__copyright__ = "Copyright 2014, JHU/APL"
__license__ = "Apache 2.0"


import sys, os, argparse, time
import pdb

import numpy as np
import scipy.io

import emlib



def get_args():
    """Command line parameters for the 'deploy' procedure.
    """
 
    parser = argparse.ArgumentParser()

    #----------------------------------------
    # Parameters for defining the neural network
    #----------------------------------------
    parser.add_argument('-s', dest='solver', type=str,
                        default=os.path.join('caffe_files', 'n3-net.prototxt'), 
                        help='Caffe solver prototxt file used during training')
    parser.add_argument('-m', dest='model', type=str, required=True,
                        help='Model weights file generated during training')
    parser.add_argument('-gpu', dest='gpu', type=int, default=-1,
                        help='Run in GPU mode on given device ID')

    #----------------------------------------
    # Data set parameters.  Assuming here a data cube, where each xy-plane is a "slice" of the cube.
    #----------------------------------------
    parser.add_argument('-X', dest='dataFileName', type=str, default='test-volume.tif',
                        help='Name of the file containing the membrane data (i.e. X)')
    parser.add_argument('-M', dest='maskFileName', type=str, default='',
                        help='Name of the file containing a pixel evaluation mask (optional)')
    parser.add_argument('--eval-slices', dest='evalSliceExpr', type=str, default='',
                        help='A python-evaluatable string indicating which z-slices should be evaluated (default is to process all slices)')
    parser.add_argument('--yhat-file', dest='outFileNameY', type=str, default='',
                        help='Probability output file (full path)')
    parser.add_argument('--feature-file', dest='outFileNameX', type=str, default='',
                        help='Features output file (optional)')

    return parser.parse_args()



def _eval_cube(net, X, M, batchDim, extractFeat=''):
    """
      PARAMETERS:
        net      - a Caffe network object
        X        - A 3d tensor of data to evaluate
        M        - A boolean 3d tensor mask; 1 := evaluate the corresponding pixel
        batchDim - Length 2 vector of tile [width,height].  
      
      RETURN VALUES:
        Yhat   - a tensor with dimensions (#classes, ...)   where "..." denotes data cube dimensions
        Xprime - a tensor with dimensions (#features, ...)  where "..." denotes data cube dimensions
    """
    
    assert(batchDim[2] == batchDim[3])  # currently we assume tiles are square

    #--------------------------------------------------
    # initialize variables and storage needed in the processing loop below 
    #--------------------------------------------------
    tileRadius = int(batchDim[2]/2)
    Xi = np.zeros(batchDim, dtype=np.float32)
    yDummy = np.zeros((batchDim[0],), dtype=np.float32) 
    cnnTime = 0.0                                 # time spent doing core CNN operations
    nClasses = net.blobs['prob'].data.shape[1]    # *** Assumes a layer called "prob"

    # allocate memory for return values
    Yhat = -1*np.ones((nClasses, X.shape[0], X.shape[1], X.shape[2]))
    if len(extractFeat):
        nFeats = net.blobs[extractFeat].data.shape[1] 
        Xprime = np.zeros((nFeats, X.shape[0], X.shape[1], X.shape[2]))
    else:
        Xprime = None
        
    print "[deploy]: Yhat shape: %s" % str(Yhat.shape)
    if Xprime is not None:
        print "[deploy]: Extracting features from layer: %s" % extractFeat
        print "[deploy]: Xprime shape:                   %s" % str(Xprime.shape)
    sys.stdout.flush()

    #--------------------------------------------------
    # process the cube
    #--------------------------------------------------
    tic = time.time()
    lastChatter = None
 
    for Idx, pct in emlib.interior_pixel_generator(X, tileRadius, batchDim[0], mask=M):
        # populate the mini-batch buffer
        for jj in range(Idx.shape[0]):
            a = Idx[jj,1] - tileRadius
            b = Idx[jj,1] + tileRadius + 1
            c = Idx[jj,2] - tileRadius
            d = Idx[jj,2] + tileRadius + 1
            Xi[jj, 0, :, :] = X[ Idx[jj,0], a:b, c:d ]

        # CNN forward pass
        _tmp = time.time()
        net.set_input_arrays(Xi, yDummy)
        out = net.forward()
        yiHat = out['prob']
        cnnTime += time.time() - _tmp

        # On some version of Caffe, yiHat is (batchSize, nClasses, 1, 1)
        # On newer versions, it is natively (batchSize, nClasses)
        # The squeeze here is to accommodate older versions
        yiHat = np.squeeze(yiHat) 

        # store the per-class probability estimates.
        #
        # * Note that on the final iteration, the size of yiHat may not match
        #   the remaining space in Yhat (unless we get lucky and the data cube
        #   size is a multiple of the mini-batch size).  This is why we slice
        #   yijHat before assigning to Yhat.
        for jj in range(nClasses):
            yijHat = yiHat[:,jj]                   # get slice containing probabilities for class j
            assert(len(yijHat.shape)==1)           # should be a vector (vs tensor)
            Yhat[jj, Idx[:,0], Idx[:,1], Idx[:,2]] = yijHat[:Idx.shape[0]]   # (*)

        # for features: net.blobs['ip1'].data  should be (100,200,1,1) for batch size 100, ip output size 200
        if Xprime is not None:
            for jj in range(nFeats):
                Xprimejj = np.squeeze(net.blobs[extractFeat].data[:,jj,:,:])  # feature jj, all objects
                assert(len(Xprimejj.shape)==1)           # should be a vector (vs tensor)
                Xprime[jj, Idx[:,0], Idx[:,1], Idx[:,2]] = Xprimejj[:Idx.shape[0]]

        # provide feedback on progress so far    
        elapsed = (time.time() - tic) / 60.
        if (lastChatter is None) or ((elapsed - lastChatter) > 2):
            lastChatter = elapsed
            print "[deploy]: processed pixel at index %s (%0.2f min elapsed; %0.2f CNN min)" % (str(Idx[-1,:]), elapsed, cnnTime/60.)
            sys.stdout.flush()

    print('[deploy]: Finished processing cube.  Net time was: %0.2f min (%0.2f CNN min)' % (elapsed, cnnTime/60.))

    return Yhat, Xprime


    

if __name__ == "__main__":
    args = get_args()
    
    import caffe
    from caffe.proto import caffe_pb2
    from google.protobuf import text_format

    #----------------------------------------
    # parse information from the prototxt files
    #----------------------------------------
    workDir, solverFn = os.path.split(args.solver)
    if len(workDir):
        os.chdir(workDir)

    solverParam = caffe_pb2.SolverParameter()
    text_format.Merge(open(solverFn).read(), solverParam)

    netFn = str(solverParam.net)          # unicode->str to avoid caffe API problems
    netParam = caffe_pb2.NetParameter()
    text_format.Merge(open(netFn).read(), netParam)
 
    batchDim = emlib.infer_data_dimensions(netFn)
    print('[deploy]: batch shape: %s' % str(batchDim))

    if len(args.outFileNameY):
        outFileNameY = args.outFileNameY
    else:
        outFileNameY = os.path.join(os.path.split(args.dataFileName)[0], 'Yhat_' + os.path.split(args.dataFileName)[-1])
    outFileNameX = args.outFileNameX
    print('[deploy]: probability output file: %s' % outFileNameY)
    print('[deploy]: features output file:    %s' % outFileNameX)
 
    #----------------------------------------
    # Load and preprocess data set
    #----------------------------------------
    X = emlib.load_cube(args.dataFileName, np.float32)

    # mirror edges of images so that every pixel in the original data set can act
    # as a center pixel of some tile    
    borderSize = int(batchDim[2]/2)
    X = emlib.mirror_edges(X, borderSize)

    if len(args.evalSliceExpr):  # optional: pare down to a subset of slices
        idx = eval(args.evalSliceExpr)
        X = X[idx,:,:]
    print('[deploy]: data shape: %s' % str(X.shape))
    
    # There may be reasons for not evaluating certain pixels.
    # The mask allows the caller to specify which pixels to omit.
    if len(args.maskFileName):
        Mask = emlib.load_cube(args.maskFileName, dtype=np.bool)
        Mask = emlib.mirror_edges(Mask, borderSize)
    else:
        Mask = np.ones(X.shape, dtype=np.bool)
        
    if np.any(Mask == 0):
        nz = np.sum(Mask==0)
        print('[deploy]: mask is omitting %0.2f%% of the raw data' % (100 * nz / np.prod(Mask.shape)))
    print('[deploy]: mask shape: %s' % str(Mask.shape))
    assert(np.all(Mask.shape == X.shape))

    #----------------------------------------
    # Create the Caffe network
    #----------------------------------------
    # UPDATE: 5/28/2015: Net constructor requires a new third argument - the phase - in the latest caffe.
    # I suppose is is in leiu of calling set_phase() below.
    try:
        net = caffe.Net(netFn, args.model)
    except: # ArgumentError
        phaseTest = 1  # XXX: verify 1 is the correct value for test phase
        net = caffe.Net(netFn, args.model, phaseTest)
    
    for name, blobs in net.params.iteritems():
        print("%s : %s" % (name, blobs[0].data.shape))
        
    # specify training mode and CPU or GPU
    # (even though we are not training, we use training mode because test mode
    #  is not compatible with the data input layer as of this writing)
    if args.gpu >= 0:
        isModeCPU = False   # command line overrides solver file
        gpuId = args.gpu
    else:
        isModeCPU = (solverParam.solver_mode == solverParam.SolverMode.Value('CPU'))
        gpuId = 0
        
    # Note that different Caffe APIs put this function different places (module vs net object)
    print('[deploy]: CPU mode = %s' % isModeCPU)
    try:
        if not isModeCPU:
            caffe.set_mode_gpu()
            caffe.set_device(gpuId)
        else:
            caffe.set_mode_cpu()
    except AttributeError:
        if not isModeCPU:
            net.set_mode_gpu()
            net.set_device(gpuId)
        else:
            net.set_mode_cpu()
        
    # Same API-related issues with setting the phase to "train".  An
    # additional complication here is that newer pycaffe (May 28,
    # 2015) does not seem to even have a train phase.
    try:
        caffe.set_phase_train()
    except AttributeError:
        try:
            net.set_phase_train()
        except AttributeError:
            pass # hopefully this is a version of Caffe that doesn't require train mode...

    sys.stdout.flush()
    
    #----------------------------------------
    # Do it
    #----------------------------------------
    extractFeat = 'ip1' if len(outFileNameX) else ''
    Yhat, Xprime = _eval_cube(net, X, Mask, batchDim, extractFeat=extractFeat)
 
    # discard border/mirroring
    Yhat = Yhat[:, :, borderSize:(-borderSize), borderSize:(-borderSize)]
     
    print('[deploy]: Finished.  Saving estimates...')
    np.save(outFileNameY, Yhat)
    scipy.io.savemat(outFileNameY+".mat", {'Yhat' : Yhat})

    if Xprime is not None:
        Xprime = Xprime[:, :, borderSize:(-borderSize), borderSize:(-borderSize)]
        np.save(outFileNameX, Xprime)
        scipy.io.savemat(outFileNameX+".mat", {'Xprime' : Xprime})
    
    print('[deploy]: exiting...')
