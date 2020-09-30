from __future__ import division
import sys
from mofapy2.core.nodes.variational_nodes import *
from mofapy2.core.nodes.Kc_node import Kc_Node
from mofapy2.core.nodes.Kg_node import Kg_Node
from mofapy2.core.gp_utils import *
import scipy as s
from mofapy2.core import gpu_utils
import pandas as pd
# from fastdtw import fastdtw
from dtw import dtw # note this is dtw-python not dtw
import copy
import gpytorch
import torch
from mofapy2.core.distributions.multi_task_GP import MultitaskGPModel, ELBO, myMultitaskGaussianLikelihood
from gpytorch.likelihoods import MultitaskGaussianLikelihood
import mofapy2.core.gp_utils as gp_utils


# TODO:
# - Sigma Node could be a general class with group_model_kron, _nokron and no_group nodes as subclasses,
#       they are now all cases in this Sigma node
# - implement warping for more than one covariate
# - add explicit ELBO and gradient calculuations for optimization

class Sigma_Node(Node):
    """
    Sigma node to optimises the GP hyperparameters for each factor and
    perform alignment of covariates per group.

    The covariance matrix is modelled as a Kronecker product of a (low-rank) group kernel
    and a covariate kernel where possible:
    Sigma = (1-zeta) * KG \otimes KC + zeta * I

    PARAMETERS
    ----------
    dim: dimensionality of the node (= number of latent factors)
    sample_cov: covariates for construction of the covariance matrix from distances (array of length G x C)
    groups: group label of each observation (array of length G)
    start_opt: in which iteration to start with optimizing the GP hyperparameters
    n_grid: number of grid points to optimize the lengthscale on
    idx_inducing: Index of inducing points (default None - models the full kernel matrix)
    warping: Boolean, whether to perform warping of covariates across groups in the latent space
    warping_freq: how often to perform warping, every n-th iteration
    warping_ref: reference group for warping
    warping_open_begin: Allow free beginning for the warped covariates in each group?
    warping_open_end:  Allow free end for the warped covariates in each group?
    opt_freq: how often to hyperparameter optimization, every n-th iteration
    rankx: rank of group covariance matrix \sum_rank x^T x
    sigma_const offset of group covariance matrix KG = \sum_rank x^T x + sigma constant diagonal or variable?
    model_groups: whether to use a group kernel on top of the covariate kernel?
    """
    def __init__(self, dim, sample_cov, groups, start_opt=20, n_grid=10, idx_inducing = None,
                 warping = False, warping_freq = 20, warping_ref = 0, warping_open_begin = True,
                 warping_open_end = True, opt_freq = 10, rankx = None, sigma_const = True,
                 model_groups = False):
        super().__init__(dim)

        # dimensions and inputs
        self.use_gradients = True
        self.mini_batch = None
        self.sample_cov = sample_cov
        self.sample_cov_transformed = copy.copy(sample_cov)         # keep original covariate in place
        self.group_labels = groups

        # covariate kernel is initialized after first warping
        self.N = sample_cov.shape[0]                                # total number of observation (C*G in the complete case, otherwise less)
        self.K = dim[0]                                             # number of factors

        # hyperparameter optimization
        self.start_opt = start_opt
        self.n_grid = n_grid
        self.iter = 0                                               # counter of iteration to keep track when to optimize lengthscales
        self.zeta = np.ones(self.K)                                 # noise hyperparameter, corresponds to idenity matrices (and the init of SimgaInv terms)
        self.gridix = np.zeros(self.K, dtype = np.int8)             # index of the lengthscale grid values to use per factor
        self.struct_sig = np.zeros(self.K)                          # store ELBO improvements compared to diagonal covariance
        self.opt_freq = opt_freq
        assert self.start_opt % self.opt_freq == 0,\
            "start_opt should be a multiple of opt_freq"            # to ensure in the first opt. step optimization is performed

        # initialize group kernel
        self.model_groups = model_groups
        self.groupsidx = pd.factorize(self.group_labels)[0]  # for each sample gives the idx in groups
        self.groups = np.unique(self.group_labels)  # distinct group labels

        if self.model_groups:
            self.G = len(self.groups)  # number of groups
            if rankx is None:
                # rankx = np.min([np.max([1, np.floor(np.log(self.G)).astype(np.int64)]), 5])
                if self.G < 50:
                    rankx = 1
                else:
                    rankx = 2

            # check: has Kronecker structure?
            if warping:
                self.kronecker = False
                print("Note: When learning an alignments and group covariance jointly inference might be slow.")
            elif idx_inducing is not None:
                self.kronecker = False # TODO select inducing points to ensure kronecker structure
            else:
                self.kronecker = np.all([np.all(self.sample_cov_transformed[self.groupsidx == 0] ==
                                         self.sample_cov_transformed[self.groupsidx == g]) for g in
                                         range(self.G)])
                if not self.kronecker:
                    print("Warning: Data has no Kronecker structure (groups \otimes covariates) - inference might be slow."
                          "If possible and no alignment required, reformat your data to have samples with identical covariates across groups.")

            self.initKg(rank = rankx, sigma_const= sigma_const, spectral_decomp = self.kronecker)

        else:
            # all samples are modelled jointly in the covariate kernel
            self.Kg = None
            self.kronecker = True
            self.G = 1

        # warping
        self.warping = warping
        self.G4warping = len(self.groups) # number of groups to consider for warping (if no group kernel this differs from self.G)
        assert warping_ref < self.G4warping,\
            "Reference group not correctly specified, exceeds the number of groups."
        self.reference_group = warping_ref
        self.warping_freq = warping_freq
        self.warping_open_begin = warping_open_begin
        self.warping_open_end = warping_open_end
        if self.warping:
            assert self.start_opt % self.warping_freq == 0,\
                "start_opt should be a multiple of warping_freq"            # to ensure in the first opt. step alignment is performed

        # sparse GPs
        self.idx_inducing = idx_inducing
        if not self.idx_inducing is None:
            self.Nu = len(idx_inducing)
        else:
            self.Nu = self.N    # dimension to use for Sigma^(-1)

        # initialize covariate kernel if no warping, otherwise recalculation after each warping required
        # if no Kronecker structure no eigendecomposition required
        if not self.warping:
            if self.idx_inducing is None:
                self.initKc(self.sample_cov_transformed, spectral_decomp = self.kronecker)
            else:
                self.initKc(self.sample_cov_transformed[self.idx_inducing], cov4grid = self.sample_cov_transformed, spectral_decomp = self.kronecker) #use all point to determine grid limits
        else:
            self.Kc = None # initialized later

        # initialize Sigma terms (unstructured)
        self.Sigma_inv = np.zeros([self.K, self.Nu, self.Nu])
        self.Sigma = np.zeros([self.K, self.N, self.N])
        for k in range(self.K):
            self.Sigma_inv[k, :, :] = np.eye(self.Nu)
            self.Sigma[k, :, :] = np.eye(self.N)

        self.Sigma_inv_logdet = np.zeros(self.K)

        # exclude cases not covered
        if self.model_groups and (self.idx_inducing is not None):
            print("The option model_groups has not been tested in conjunction with sparse GPs")
            # sys.exit()
        if self.warping and self.idx_inducing is not None :
            print("The option warping cannot be used jointly with sparse GPs.")
            sys.exit()


    def initKc(self, transformed_sample_cov, cov4grid = None, spectral_decomp = True):
        """
        Method to initialize the components required for the covariate kernel
        """
        if self.model_groups:
            self.covariates = np.unique(transformed_sample_cov, axis=0)  # distinct covariate values
            # non unique if no group model
            self.covidx = np.asarray([np.where((self.covariates == transformed_sample_cov[j, :]).all(axis=1))[0].item()
                                      for j in range(self.Nu)])  # for each sample gives the idx in covariates
        else:
            self.covariates = transformed_sample_cov # all covariate values


        self.C = self.covariates.shape[0]  # number of covariate values

        # set covariate kernel
        self.Kc = Kc_Node(dim=(self.K, self.C), covariates = self.covariates, n_grid = self.n_grid, cov4grid = cov4grid, spectral_decomp = spectral_decomp)


    def initKg(self, rank, sigma_const, spectral_decomp):
        """
        Method to initialize the group kernel
        """
        # set group kernel
        self.Kg = Kg_Node(dim=(self.K, self.G), rank = rank, sigma_const = sigma_const, spectral_decomp = spectral_decomp)


    def precompute(self, options):
        gpu_utils.gpu_mode = options['gpu_mode']

    
    def get_components(self,k):
        """
        Method to fetch ELBO-optimal covariance matrix components for a given factor k
        """
        Vc, Dc = self.Kc.get_kernel_components_k(k)

        if self.model_groups:
            Vg, Dg = self.Kg.get_kernel_components_k(k)
        else:
            Vg = np.array([1]); Dg = np.array([1])

        zeta = self.get_zeta()[k]

        return {'Vc' : Vc, 'Vg' : Vg, 'Dc' : Dc, 'Dg' : Dg, 'zeta' : zeta}

    def calc_sigma_terms(self, only_inverse = False):
        """
        Method to compute the inverse of sigma and its log determinant based on the spectral decomposition
         of the kernel matrices for all factors
        """
        for k in range(self.K):
            self.calc_sigma_terms_k(k, only_inverse)

    def calc_sigma_terms_k(self, k, only_inverse = False):
        """
        Method to compute the inverse of sigma and its log determinant based on the spectral decomposition
         of the kernel matrices for a given factor k
        """
        if self.zeta[k] == 1:
            self.Sigma_inv[k, :, :] = np.eye(self.Nu)
            self.Sigma_inv_logdet[k] = 1
            self.Sigma[k, :, :] = np.eye(self.N)
        else:
            if self.kronecker:
                components = self.get_components(k)
                term1 = np.kron(components['Vg'], components['Vc'])
                term2diag = 1/ (np.repeat(components['Dg'], self.C) * np.tile(components['Dc'], self.G) + self.zeta[k] / (1-self.zeta[k]))
                term3 = np.kron(components['Vg'].transpose(), components['Vc'].transpose())
                self.Sigma_inv[k, :, :] = 1 / (1 - self.zeta[k]) * gpu_utils.dot(gpu_utils.dot(term1, np.diag(term2diag)), term3)
                self.Sigma_inv_logdet[k] = - self.Nu * s.log(1 - self.zeta[k]) + s.log(term2diag).sum()

                # update Sigma as well (not required in ELBO of Z but for updates of Z|U in the sparse GP setting and to obtain valid expectation of Sigma)
                if not only_inverse:
                    if self.idx_inducing is not None:
                        self.update_Sigma_complete_k(k)
                    else:
                        components = self.get_components(k)
                        term1 = np.kron(components['Vg'], components['Vc'])
                        term2diag = np.repeat(components['Dg'], self.C) * np.tile(components['Dc'], self.G) + \
                                    self.zeta[k] / (1 - self.zeta[k])
                        term3 = np.kron(components['Vg'].transpose(), components['Vc'].transpose())
                        self.Sigma[k, :, :] = (1 - self.zeta[k]) * gpu_utils.dot(term1,
                                                                            gpu_utils.dot(np.diag(term2diag),
                                                                                          term3))
            else:
                if self.idx_inducing is not None:
                    self.update_Sigma_complete_k(k)
                    self.Sigma_inv[k, :, :] = np.linalg.inv(self.Sigma[k, self.idx_inducing, :][:,self.idx_inducing])
                    self.Sigma_inv_logdet[k] = np.linalg.slogdet(self.Sigma_inv[k, :, :])[1]
                else:
                    self.Sigma[k, :, :] = (1 - self.zeta[k]) * self.Kc.Kmat[self.Kc.get_best_lidx(k), self.covidx,:][:, self.covidx] * self.Kg.Kmat[k,self.groupsidx,:][:,self.groupsidx] + self.zeta[k] * np.eye(self.N)
                    self.Sigma_inv[k, :, :] = np.linalg.inv(self.Sigma[k, :, :])
                    self.Sigma_inv_logdet[k] = np.linalg.slogdet(self.Sigma_inv[k, :, :])[1]


    def getInverseTerms_k(self, k):
        """ 
         Method to fetch ELBO-optimal inverse covariance matrix and its determinant for a given factor k
        """
        return {'inv': self.Sigma_inv[k,:,:], 'inv_logdet':  self.Sigma_inv_logdet[k]}


    def getInverseTerms(self):
        """ 
        Method to fetch ELBO-optimal inverse covariance matrix and its determinant for all factors
        """
        return {'inv': self.Sigma_inv, 'inv_logdet': self.Sigma_inv_logdet}


    def get_ls(self):
        """
        Method to fetch ELBO-optimal length-scales
        """
        ls = self.Kc.get_ls()
        return ls


    def get_x(self):
        """
        Method to get low rank group covariance matrix
        """
        x = self.Kg.get_x()
        return x

    def get_sigma(self):
        """
        Method to get sigma hyperparametr
        """
        sigma = self.Kg.get_sigma()
        return sigma

    def update_Sigma_complete_k(self, k):
        """
        Method to update the entire covariance matrix for the k-th factor (used in context of sparse GPs to pass to Z|U node)
        """
        if self.zeta[k] == 1 or self.Kc is None:
                self.Sigma[k, :, :] = np.eye(self.N)
        else:
            Kc_k = self.Kc.eval_at_newpoints_k(self.sample_cov_transformed, k)
            self.Sigma[k, :, :] = (1 - self.zeta[k]) * Kc_k  + self.zeta[k] * np.eye(self.N)


    def getExpectation(self):
        """
        Method to get sigma hyperparameter
        """
        return self.Sigma

    def getExpectations(self):
        """
        Method to get covariance matrix and inverse terms (used for sparse GPs)
        """
        Sigma = self.getExpectation()
        invTerms = self.getInverseTerms()
        return {'cov' : Sigma, 'inv': invTerms['inv'], 'inv_logdet': invTerms['inv_logdet']}

    def get_zeta(self):
        """
        Method to fetch noise parameter
        """
        return self.zeta


    def getParameters(self):
        """ 
        Method to fetch ELBO-optimal length-scales, improvements compared to diagonal covariance prior and structural positions
        """
        ls = self.get_ls()
        zeta = self.get_zeta()

        if not self.model_groups:
            Kg = np.ones([self.K, self.G, self.G])
            return {'l': ls, 'scale': 1 - zeta, 'sample_cov': self.sample_cov_transformed, 'Kg' : Kg}

        x = self.get_x()
        sigma = self.get_sigma()
        return {'l':ls, 'scale': 1-zeta, 'sample_cov': self.sample_cov_transformed,  'x': x, 'sigma' : sigma, 'Kg' : self.Kg.Kmat}


    def removeFactors(self, idx, axis=1):
        """
        Method to remove factors 
        """
        if self.Kg is not None:
            self.Kg.removeFactors(idx)
        if self.Kc is not None:
            self.Kc.removeFactors(idx)
        self.zeta = s.delete(self.zeta, axis=0, obj=idx)
        self.updateDim(0, self.dim[0] - len(idx))
        self.K = self.K - 1
        self.Sigma  = s.delete(self.Sigma, axis=0, obj=idx)
        self.Sigma_inv  = s.delete(self.Sigma_inv, axis=0, obj=idx)
        self.Sigma_inv_logdet  = s.delete(self.Sigma_inv_logdet, axis=0, obj=idx)


    def calc_neg_elbo_k(self, par, lidx, k, var):

        self.zeta[k] = par[0]
        self.Kc.set_gridix(lidx, k)

        # if required set group parameters
        if self.model_groups:
            if self.Kg.sigma_const:
                sigma = par[1]
                x = par[2:]
            else:
                sigma = par[1:(self.G+1)]
                x = par[(self.G+1):]
            assert len(x) == self.Kg.rank * self.G,\
                "Length of x incorrect: Is %s, should be  %s * %s" % (len(x), self.Kg.rank, self.G)
            x = x.reshape(self.Kg.rank, self.G)
            self.Kg.set_parameters(x=x, sigma=sigma, k=k, spectral_decomp=self.kronecker) # set and recalculate group kernel (matrix and spectral decomposition if Kronecker

        self.calc_sigma_terms_k(k, only_inverse = True)
        elbo = var.calculateELBO_k(k)

        return -elbo

    def calc_neg_elbo_grad_k(self, par, lidx, k, var):
        gradient_Sigma_zeta, gradient_Sigma_sigma, gradient_Sigma_x = self.calc_gradient_Sigma(par, lidx, k)
        gradient = [-var.calcELBOgrad_k(k, gradient_Sigma_zeta)] + \
                   [-var.calcELBOgrad_k(k, gradient_Sigma_sigma)] +\
                   [-var.calcELBOgrad_k(k, gradient_Sigma_x[i]) for i in range(len(gradient_Sigma_x))]

        return gradient

    def calc_Sigma_element(self, par, lidx, k, id1, id2):
        """
        Method to calculated elements of Sigma matrix in hyperparameters
        Only used for debugging purposes
        """
        self.zeta[k] = par[0]
        # set lengthscale parameter
        self.Kc.set_gridix(lidx, k)

        # if required set group parameters
        if self.model_groups:
            if self.Kg.sigma_const:
                sigma = par[1]
                x = par[2:]
            else:
                sigma = par[1:(self.G + 1)]
                x = par[(self.G + 1):]
            assert len(x) == self.Kg.rank * self.G, \
                "Length of x incorrect: Is %s, should be  %s * %s" % (len(x), self.Kg.rank, self.G)
            x = x.reshape(self.Kg.rank, self.G)
            self.Kg.set_parameters(x=x, sigma=sigma, k=k,
                                   spectral_decomp=self.kronecker)

        if self.kronecker:
            Vc, Dc = self.Kc.get_kernel_components_k(k)
            Kc = gpu_utils.dot(gpu_utils.dot(Vc.tranpose(), Dc), Vc)
            Vg, Dg = self.Kg.get_kernel_components_k(k)
            Kg = gpu_utils.dot(gpu_utils.dot(Vg.tranpose(), Dg), Vg)
        else:
            Kc = self.Kc.Kmat[self.Kc.get_best_lidx(k),:,:]
            Kg = self.Kg.Kmat[k,:,:]

        val = (1-self.zeta[k]) * Kc[ self.covidx, :][:,self.covidx] * Kg[self.groupsidx, :][:, self.groupsidx] + self.zeta[k] *np.eye(self.N)

        return val[id1,id2]

    def calc_gradient_Sigma(self, par, lidx, k):
        """
        Method to calculate gradients of covariance matrix wrt to hyperparameters
        """
        self.zeta[k] = par[0]
        self.Kc.set_gridix(lidx, k)

        # if required set group parameters
        if self.model_groups:
            if self.Kg.sigma_const:
                sigma = par[1]
                x = par[2:]
            else:
                sigma = par[1:(self.G + 1)]
                x = par[(self.G + 1):]
            assert len(x) == self.Kg.rank * self.G, \
                "Length of x incorrect: Is %s, should be  %s * %s" % (len(x), self.Kg.rank, self.G)
            x = x.reshape(self.Kg.rank, self.G)
            self.Kg.set_parameters(x=x, sigma=sigma, k=k,
                                   spectral_decomp=self.kronecker)  # set and recalculate group kernel (matrix and spectral decomposition if Kronecker

        # get kernel matrices
        if self.kronecker:
            Vc, Dc = self.Kc.get_kernel_components_k(k)
            Kc = gpu_utils.dot(gpu_utils.dot(Vc.tranpose(), Dc), Vc)
            Vg, Dg = self.Kg.get_kernel_components_k(k)
            Kg = gpu_utils.dot(gpu_utils.dot(Vg.tranpose(), Dg), Vg)
        else:
            Kc = self.Kc.Kmat[self.Kc.get_best_lidx(k),:,:]
            Kg = self.Kg.Kmat[k,:,:]

        # gradient wrt zeta
        gradient_Sigma_zeta = - Kc[self.covidx, :][:,self.covidx] *\
                              Kg[self.groupsidx, :][:, self.groupsidx] +\
                              np.eye(self.N)

        # gradient wrt sigma
        Gmat = Kg #np.dot(x.transpose(), x) + sigma * np.eye(self.G)
        Gmat_sqrt = np.sqrt(Gmat)
        N = np.array([[Gmat_sqrt[g,g] * Gmat_sqrt[h,h] for g in range(self.G)] for h in range(self.G)])
        AN_sigma = np.array([[-0.5 * Gmat_sqrt[g,g] / Gmat_sqrt[h,h] -0.5 * Gmat_sqrt[h,h] / Gmat_sqrt[g,g] for g in range(self.G)] for h in range(self.G)])
        N2  = np.array([[Gmat[g,g] * Gmat[h,h] for g in range(self.G)]for h in range(self.G)])
        Z =  np.dot(x.transpose(), x) # diagonal can be neglected as set to 1, gradient 0
        # AZ_sigma = 0
        diffGmat_sigma = (1-np.eye(self.G)) * Z * AN_sigma / N2
        gradient_Sigma_sigma = (1 - self.zeta[k]) *\
                               diffGmat_sigma[self.groupsidx, :][:,self.groupsidx] \
                               * Kc[self.covidx, :][:, self.covidx]

        # gradient wrt x
        drg = [[-0.5 * 1/ Gmat_sqrt[g,g] * 2 * x[r, g] for r in range(self.Kg.rank)] for g in range(self.G)]
        # below diagonal can be neglected as set to 1, gradient 0
        AN_x = [[np.outer(np.diag(Gmat_sqrt), drg[g][r] * np.eye(self.G)[g, :]) + np.outer(np.diag(Gmat_sqrt), drg[g][r] * np.eye(self.G)[g, :]).transpose() for r in range(self.Kg.rank)] for g in range(self.G)]
        AZ_x = [[np.outer(x[r, :], np.eye(self.G)[g, :]) + np.outer(x[r, :],np.eye(self.G)[g,:]).transpose() for r in range(self.Kg.rank)] for g in range(self.G)]
        diffGmat_x  = [[(1-np.eye(self.G)) * (Z * AN_x[g][r] + AZ_x[g][r] * N) / N2 for r in range(self.Kg.rank)] for g in range(self.G)]
        gradient_Sigma_x = [(1 - self.zeta[k]) *
                            diffGmat_x[g][r][self.groupsidx, :][:,self.groupsidx] *
                            Kc[self.covidx, :][:,self.covidx]
                            for r in range(self.Kg.rank) for g in range(self.G)]

        return gradient_Sigma_zeta, gradient_Sigma_sigma, gradient_Sigma_x


    def check_gradient(self, par_init, lidx, k, var):
        """
        Method to check analytical gradient were correctly calculated
        (Used for debugging purposes only)
        """
        # check gradients are calulcated correctly
        z = np.random.uniform(0, 1, len(par_init))
        s.optimize.approx_fprime(z, self.calc_neg_elbo_k, 1.4901161193847656e-08, lidx, k, var) - self.calc_neg_elbo_grad_k(z, lidx, k, var)
        a = np.zeros(len(par_init))
        for n in range(3):
            z = np.random.uniform(0, 1, len(par_init))
            gradient_Sigma_zeta, gradient_Sigma_sigma, gradient_Sigma_x = self.calc_gradient_Sigma(z, lidx, k)
            G_sigma_calc = [gradient_Sigma_zeta ]+ [gradient_Sigma_sigma] + gradient_Sigma_x
            G_sigma_approx = np.array([[s.optimize.approx_fprime(z, self.calc_Sigma_element, 1.4901161193847656e-08, lidx, k, i, j) for i in range(self.N)] for j in range(self.N)])
            # F_calc = np.array([[ self.sigma_fun(z, lidx, k, var, i, j) for i in range(self.N)] for j in range(self.N)])
            for l in range(len(a)):
                a[l] = np.max(np.abs(G_sigma_approx[:,:,l] - G_sigma_calc[l]))
            print("Maximal differences in gradient of Sigma for lidx", lidx,":", a)
            print("Difference in ELBO gradient:", s.optimize.check_grad(self.calc_neg_elbo_k, self.calc_neg_elbo_grad_k, z), lidx, k, var))

    def optimise(self):
        """
        Method to find for each factor the lengthscale parameter and remaining hyperparameters that optimises the ELBO of the factor.
        The optimization can be carried out on a per-factor basis (not required on all combinations) as latent variables are independent in the elbo
        """

        # get Z/U node of Markov blanket
        if not self.idx_inducing is None:
            var = self.markov_blanket['U']
        else:
            var = self.markov_blanket['Z']

        K = var.dim[1]
        assert K == len(self.zeta) and K == self.K,\
            'problem in dropping factor'

        # perform DTW to align groups
        if self.warping and self.G4warping > 1 and self.iter % self.warping_freq == 0:
            if not self.idx_inducing is None:
                Zvar = self.markov_blanket['U'].markov_blanket['Z']  # use all factor values for alignment #TODO clean this, move alignment to Znode?
            else:
                Zvar = var

            ZE = Zvar.getExpectation()

            self.align_sample_cov_dtw(ZE)
            print("Covariates were aligned between groups.")

        # optimise hyperparamters of GP
        if self.iter % self.opt_freq == 0:
            for k in range(K):

                best_zeta = -1
                best_elbo = -np.Inf

                # set initial values for optimization
                if self.model_groups:
                    # par = (zeta, sigma, x, lidx), loop over lidx
                    par_init = np.hstack([self.zeta[k], self.get_sigma()[k].flatten(), self.get_x()[k,:,:].flatten()])
                else:
                    # par = (zeta, lidx), loop over lidx
                    par_init = self.zeta[k]

                # use grid search to optimise lengthscale hyperparameters
                for lidx in range(self.n_grid):
                    if self.model_groups:
                        if self.Kg.sigma_const:
                            bounds = [(1e-10, 1-1e-10)] # zeta
                            bounds = bounds + [(1e-10, 1-1e-10)]  # sigma
                            bounds = bounds + [(-1,1)] * self.G *  self.Kg.rank # x
                            par0 = [1, 1] + [0] * self.G *  self.Kg.rank # parameters for zero lengthscale/scale (unstrucutred)
                            # par0 = [1, 0] + [1/np.sqrt(self.Kg.rank)] * self.G *  self.Kg.rank # parameters for zero lengthscale (fully connected groups)
                        else:
                            bounds = [(1e-10, 1-1e-10)] # zeta
                            bounds = bounds + [(1e-10, 1-1e-10)] * self.G  # sigma
                            bounds = bounds + [(-1,1)] * self.G *  self.Kg.rank # x
                            par0 = [1] + [1] * self.G + [0] * self.G * self.Kg.rank
                            # par0 = [1] + [0] * self.G + [1/np.sqrt(self.Kg.rank)] * self.G * self.Kg.rank
                    else:
                        bounds = [(1e-10, 1-1e-10)] # zeta
                        par0 = [1]
                    par0 = np.array(par0)

                    # make sure initial parameters are in admissible region
                    par_init = np.max(np.vstack([par_init, [bounds[k][0] for k in range(len(bounds))]]), axis = 0)
                    par_init = np.min(np.vstack([par_init, [bounds[k][1] for k in range(len(bounds))]]), axis = 0)

                    # optimize
                    if self.use_gradients and self.model_groups: # without group model there is only a single parameter (zeta) and analytical gradients not required
                        self.check_gradient(par_init, lidx, k, var)
                        res = s.optimize.minimize(self.calc_neg_elbo_k, args=(lidx, k, var), x0 = par_init, bounds=bounds, jac = self.calc_neg_elbo_grad_k) # L-BFGS-B
                    else:
                        res = s.optimize.minimize(self.calc_neg_elbo_k, args=(lidx, k, var), x0=par_init, bounds=bounds)
                    elbo = -res.fun

                    # for lidx = 0: zeta can be non-identifiable, use unstructured model (do not model soley group structure)
                    if lidx == 0:
                        best_param4ls = par0
                    else:
                        best_param4ls = res.x

                    # for zeta = 1: K_G and K_C are non-identifiable, use unstructured model
                    if best_param4ls[0] == 1:
                        best_param4ls = par0
                        lidx = 0

                    if elbo > best_elbo:
                        best_elbo = elbo
                        best_lidx = lidx
                        best_zeta = best_param4ls[0]

                        if self.model_groups:
                            if self.Kg.sigma_const:
                                best_sigma = best_param4ls[1]
                                best_x = best_param4ls[2:].reshape(self.Kg.rank, self.G)
                            else:
                                best_sigma = best_param4ls[1:(self.G+1)]
                                best_x = best_param4ls[(self.G+1):].reshape(self.Kg.rank, self.G)

                # save optimized kernel paramters
                self.Kc.set_gridix(best_lidx, k)
                if self.model_groups:
                    self.Kg.set_parameters(x=best_x, sigma=best_sigma, k=k, spectral_decomp=self.kronecker)

                self.zeta[k] = best_zeta

            self.calc_sigma_terms(only_inverse = False)
            print('Sigma node has been optimised: Lengthscales =', self.get_ls(), ', Scale =',  1-self.get_zeta())

    def align_sample_cov_dtw(self, Z):
        """
        Method to perform DTW between groups in the factor space.
        The set of possible values for covaraites cannot be expaned (all need to be contained in the reference group)
        Thus, this does not requrie an update of Kc but only of indices mapping samples to covariates.
        """
        paths = []
        for g in range(self.G4warping):
            if g is not self.reference_group:
                # reorder by covariate value to ensure monotonicity constrains are correctly placed
                idx_ref_order = np.argsort(self.sample_cov[self.groupsidx == self.reference_group,0])
                idx_query_order = np.argsort(self.sample_cov[self.groupsidx == g,0])
                # allow for partial matching (no corresponding end and beginning)
                step_pattern = "asymmetric" if self.warping_open_begin or self.warping_open_end else "symmetric2"
                alignment = dtw(Z[self.groupsidx == g, :][idx_query_order,:], Z[self.groupsidx == self.reference_group, :][idx_ref_order,:],
                                open_begin = self.warping_open_begin, open_end = self.warping_open_end, step_pattern=step_pattern)
                query_idx = alignment.index1 # dtw-python
                ref_idx = alignment.index2
                ref_val = self.sample_cov[self.groupsidx == self.reference_group, 0][idx_ref_order][ref_idx]
                idx = np.where(self.groupsidx == g)[0][idx_query_order][query_idx]
                self.sample_cov_transformed[idx, 0] = ref_val

        # covariate kernel need to be re-initialized after each warping
        self.initKc(self.sample_cov_transformed, spectral_decomp=self.kronecker)


    def updateParameters(self, ix, ro):
        """
        Public method to update the nodes parameters
        """
        self.iter += 1
        if self.iter >= self.start_opt:
            self.optimise()

    def calculateELBO(self): # no contribution to ELBO
        return 0

    def get_mini_batch(self):
        """
        Method to fetch minibatch
        """
        if self.mini_batch is None:
            return self.getExpectations()
        else:
            return self.mini_batch

# # gpytorch based implementation of the above - expects data with Kronecker structure
# class Sigma_Node_torch(Sigma_Node):
#     def __init__(self, dim, sample_cov, groups, start_opt=20, n_grid=10, idx_inducing=None,
#                  warping=False, warping_freq=20, warping_ref=0, warping_open_begin=True,
#                  warping_open_end=True, opt_freq=10, rankx=None, sigma_const=True,
#                  model_groups=False, torch_seed = 7823982, gp_iter = 200, verbose = False):
#         super().__init__(dim, sample_cov, groups, start_opt, n_grid, idx_inducing,
#                  warping, warping_freq, warping_ref, warping_open_begin,
#                  warping_open_end, opt_freq, rankx, sigma_const,
#                  model_groups)
#
#         self.gp = [None] * self.K
#         self.likelihood = [myMultitaskGaussianLikelihood(num_tasks=self.G, noise_constraint=gpytorch.constraints.Interval(1e-4,1 - 1e-4), rank = 0)] * self.K # noise constraint from original mutltiakslik, no correlation model for noise (rank = 0),  ELBO instead of MLL
#         # self.likelihood = [MultitaskGaussianLikelihood(num_tasks=self.G, rank = 0)] * self.K # basic multitaks model (no ELBO term and noise/scale dependence)
#
#         self.l_limits = gp_utils.get_l_limits(sample_cov)
#         self.sigma = np.array([np.nan] * self.K)
#         self.ls = np.array([np.nan] * self.K)
#         self.Gmat = [np.nan] * self.K
#         self.B = [np.nan] * self.K
#         self.gp_iter = gp_iter
#         self.verbose = verbose
#         torch.manual_seed(torch_seed)
#
#         # avoid initiilisation of the following - make a more gneral superclass
#         self.rank = self.Kg.rank
#         self.Kg = None
#         self.Kc = None
#
#     # def objective(self, par, lidx, k, var):
#     #     """"
#     #     Reimplements the ELBO as implemented in Z node here use pure pytorch for optimization
#     #     """
#     #     0.5 *
#
#     def optimise(self):
#         """
#         Method to find for each factor the lengthscale parameter that optimises the ELBO of the factor.
#         The optimization can be carried out on a per-factor basis (not required on all combinations) as latent variables are independent in the elbo
#         """
#
#         # get Z/U node of Markov blanket
#         if not self.idx_inducing is None:
#             var = self.markov_blanket['U']
#         else:
#             var = self.markov_blanket['Z']
#
#         K = var.dim[1]
#         assert K == len(self.zeta) and K == self.K, \
#             'problem in dropping factor'
#
#         # perform DTW to align groups
#         if self.warping and self.G4warping > 1 and self.iter % self.warping_freq == 0:
#             if not self.idx_inducing is None:
#                 Zvar = self.markov_blanket['U'].markov_blanket['Z']  # use all factor values for alignment
#             else:
#                 Zvar = var
#
#             ZE = Zvar.getExpectation()
#             self.align_sample_cov_dtw(ZE)
#             print("Covariates were aligned between groups.")
#
#         # optimise hyperparamters of GP
#         if self.iter % self.opt_freq == 0:
#             for k in range(K):
#
#                 best_i = -1
#                 best_zeta = -1
#                 best_elbo = -np.Inf
#
#                 # set initial values for optimization
#                 ZE = copy.deepcopy(var.getExpectation())
#                 ZE = torch.as_tensor(ZE, dtype=torch.float32)
#                 ytrain = torch.stack([ZE[self.groupsidx == g, k] for g in range(self.G)])
#                 xtrain = torch.as_tensor(self.sample_cov_transformed[self.groupsidx == 0], dtype=torch.float32) # TODO only works for kroncker structure RIGHT DIMS?
#
#                 # self.gp[k] = commonMultitaskGPModel(train_x=xtrain, train_y=ytrain,
#                 #                               likelihood=self.likelihood[k],
#                 #                               n_tasks=self.G, rank=self.rank)
#
#                 self.gp[k] = MyMultitaskGPModel(train_x=xtrain, train_y=ytrain,
#                                               likelihood=self.likelihood[k],
#                                               n_tasks=self.G, rank=self.rank,
#                                               var_constraint = gpytorch.constraints.Interval(1e-10,1 - 1e-10),
#                                               covar_factor_constraint=gpytorch.constraints.Interval(-1 + 1e-10,1 - 1e-10),
#                                               lengthscale_constraint = gpytorch.constraints.Interval(self.l_limits[0], self.l_limits[1]))#gpytorch.constraints.Interval(-1 + 1e-10, 1 - 1e-10))
#
#
#                 training_iterations = self.gp_iter
#
#                 # Find optimal model hyperparameters
#                 self.gp[k].train()
#                 self.likelihood[k].train()
#
#                 # Use the adam optimizer
#                 optimizer = torch.optim.Adam([
#                     {'params': self.gp[k].parameters()},  # Includes GaussianLikelihood parameters
#                 ], lr=0.1)
#
#                 # "Loss" for GP given by ELBO
#                 ZCov = copy.deepcopy(torch.as_tensor(var.getExpectations()['cov'][k], dtype=torch.float32))
#                 elbo = ELBO(self.likelihood[k], self.gp[k], ZCov)
#                 # mll = gpytorch.mlls.ExactMarginalLogLikelihood(self.likelihood[k], self.gp[k])
#
#                 for i in range(training_iterations):
#                     optimizer.zero_grad()
#                     output = self.gp[k](xtrain)
#                     loss = -elbo(output, ytrain)
#                     # loss = -mll(output, ytrain)
#                     loss.backward()
#                     if self.verbose:
#                         print('Iter %d/%d - Loss: %.3f' % (i + 1, training_iterations, loss.item()))
#                     optimizer.step()
#
#                 self.gp[k].eval()
#                 self.likelihood[k].eval()
#
#                 print('Sigma node for factor %s has been optimised: ' %k)
#                 self.zeta[k] = (self.gp[k].likelihood.noise).detach().numpy().item()
#                 self.ls[k] = (self.gp[k].covar_module.data_covar_module.lengthscale).detach().numpy().item()
#                 self.sigma[k] = (self.gp[k].covar_module.task_covar_module.var).detach().numpy()
#                 self.B[k] = (self.gp[k].covar_module.task_covar_module.covar_factor).detach().numpy()
#                 # self.Gmat[k] = np.dot(self.B[k], self.B[k].transpose()) + np.diag(self.sigma[k]) #equivalent to below
#                 # self.Gmat[k] = gpytorch.lazy.LazyTensor.evaluate(self.gp[k].covar_module.task_covar_module.covar_matrix).detach().numpy()
#                 self.Gmat[k] = self.gp[k].covar_module.task_covar_module.covar_matrix.detach().numpy()
#
#                 # TODO avoid these recomputations build from in gp objects
#                 K_mixed = gpytorch.lazy.LazyTensor.evaluate(self.gp[k].covar_module(xtrain)).detach().numpy() # this is Kc \odot Kg not Kg \odot Kc
#                 Kmat = np.vstack([np.hstack(K_mixed[np.arange(self.N) % self.G == g][:, np.arange(self.N) % self.G == h] for h in range(self.G)) for g in range(self.G)])
#                 # self.gp[k](xtrain).precision_matrix
#                 # self.gp[k](xtrain).covariance_matrix
#                 assert np.max(np.abs(SE(self.covariates, self.ls[k], zeta=0)[self.covidx, :][:,self.covidx] *
#                                      self.Gmat[k][self.groupsidx, :][:,self.groupsidx] - Kmat)) < 1e-4,\
#                     "bug in kernel computation"
#
#                 self.Sigma[k, :, :] = (1 - self.zeta[k]) * Kmat + self.zeta[k] * np.eye(self.N)
#                 self.Sigma_inv[k, :, :] = np.linalg.inv(self.Sigma[k, :, :]) #TODO possible to reuse terms from gp object? - see above
#                 self.Sigma_inv_logdet[k] = np.linalg.slogdet(self.Sigma_inv[k, :, :])[1]
#
#
#             print('Inferred hyperparameters: Lengthscales =', self.ls, ', noise =', self.get_zeta())
#             print('Inferred hyperparameters: B =', self.B, ', sigma =', self.sigma)
#
#     def make_predictions(self, k, test_x):
#         # Make predictions
#         with torch.no_grad(), gpytorch.settings.fast_pred_var():
#             predictions = self.likelihood[k](self.gp[k](test_x))
#             mean = predictions.mean
#             lower, upper = predictions.confidence_region()
#
#         return mean, lower, upper
#
#     def get_zeta(self):
#         """
#         Method to fetch ELBO-optimal noise
#         """
#         return self.zeta
#
#     def get_ls(self):
#         """
#         Method to fetch ELBO-optimal length-scales
#         """
#         return self.ls
#
#     def get_x(self):
#         """
#         Method to get low rank group covariance matrix
#         """
#         return self.B
#
#     def get_sigma(self):
#         """
#         Method to get sigma hyperparametr
#         """
#         return self.sigma
#
#     def getParameters(self):
#         """
#         Method to fetch ELBO-optimal length-scales, improvements compared to diagonal covariance prior and structural positions
#         """
#         ls = self.get_ls()
#         zeta = self.get_zeta()
#
#         if not self.model_groups:
#             Kg = np.ones([self.K, self.G, self.G])
#             return {'l': ls, 'scale': 1 - zeta, 'sample_cov': self.sample_cov_transformed, 'Kg' : Kg}
#
#         x = self.get_x()
#         sigma = self.get_sigma()
#         return {'l':ls, 'scale': 1-zeta, 'sample_cov': self.sample_cov_transformed,  'x': x, 'sigma' : sigma, 'Kg' : self.Gmat}
