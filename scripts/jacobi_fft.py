from __future__ import division
import numpy as np

from matplotlib.pyplot import *

import logging
logging.basicConfig(level=logging.DEBUG)



import cmbcr
import cmbcr.utils
reload(cmbcr.beams)
reload(cmbcr.cr_system)
reload(cmbcr.precond_sh)
reload(cmbcr.precond_psuedoinv)
reload(cmbcr.precond_diag)
reload(cmbcr.precond_pixel)
reload(cmbcr.utils)
reload(cmbcr.multilevel)
reload(cmbcr)
from cmbcr.utils import *

from cmbcr import sharp
from healpy import mollzoom, mollview
from scipy.sparse import csc_matrix
#reload(cmbcr.main)

import sys
from cmbcr.cg import cg_generator

import scipy.ndimage

config = cmbcr.load_config_file('input/{}.yaml'.format(sys.argv[1]))

w = 1

nside = 16 * w
factor = 2048 // nside * w


def padvec(u):
    x = np.zeros(12 * nside**2)
    x[pick] = u
    return x

full_res_system = cmbcr.CrSystem.from_config(config, udgrade=nside, mask_eps=0.8)

full_res_system.prepare_prior()

system = cmbcr.downgrade_system(full_res_system, 1. / factor)

lmax_ninv = 2 * max(system.lmax_list)
rot_ang = (0, 0, 0)

system.set_params(
    lmax_ninv=lmax_ninv,
    rot_ang=rot_ang,
    flat_mixing=False,
    )

system.prepare_prior()
system.prepare(use_healpix=True)


rng = np.random.RandomState(1)

x0 = [
    scatter_l_to_lm(1. / system.dl_list[k]) *
    rng.normal(size=(system.lmax_list[k] + 1)**2).astype(np.float64)
    for k in range(system.comp_count)
    ]
b = system.matvec(x0)
x0_stacked = system.stack(x0)

lmax = system.lmax_list[0]
if 1:
    l = np.arange(1, 2 * lmax + 1).astype(float)
    dl = l**2
lmax_sh = dl.shape[0]
nrings = lmax + 1


if 1:
    mask_lm = sharp.sh_analysis(lmax, system.mask)
    mask_gauss = sharp.sh_synthesis_gauss(lmax, mask_lm)
    mask_gauss[mask_gauss < 0.9] = 0
    mask_gauss[mask_gauss >= 0.9] = 1
else:
    mask_gauss = np.ones((nrings, 2 * nrings))
    k = -2
    mask_gauss[(5*nrings)//12 - k :(7*nrings)//12 + k] = 0
    mask_gauss = mask_gauss.reshape(2 * nrings**2)



# Figure out the maximum value of the operator when working in SHTs...
z = np.zeros(2 * nrings**2)
z[nrings**2 + nrings] = 1
z = sharp.sh_adjoint_synthesis_gauss(lmax, z)
z *= scatter_l_to_lm(dl[:lmax + 1])
z = sharp.sh_synthesis_gauss(lmax, z)
estimated_max_sht = z[nrings**2 + nrings]


# Set up FFT operator
def cl_to_flatsky(cl, nx, ny, nl):
    out = np.zeros((nx, ny))

    l = np.sqrt(
        (np.fft.fftfreq(nx, 1. / (2 * nl))[:, None])**2
        + (np.fft.fftfreq(ny, 1. / (2 * nl))[None, :])**2)

    cl_flat = scipy.ndimage.map_coordinates(cl, l[None, :, :], mode='constant', order=1)
    return cl_flat

dl_fft = cl_to_flatsky(dl, nrings, 2 * nrings, lmax + 1)

def flatsky_synthesis(u):
    return np.fft.ifftn(u) * np.prod(u.shape)

def flatsky_adjoint_synthesis(u):
    return np.fft.fftn(u)

def flatsky_analysis(u):
    return np.fft.fftn(u) / np.prod(u.shape)

def flatsky_adjoint_analysis(u):
    return np.fft.ifftn(u)


flatsky_matvec_ratio = 1

def flatsky_matvec(u):
    u = flatsky_adjoint_synthesis(u)
    u *= dl_fft
    u = flatsky_synthesis(u).real
    return u


u = np.zeros((nrings, 2 * nrings))
u[nrings // 2, nrings] = 1
u_out = flatsky_matvec(u)
estimated_max_fft = u_out.max()

flatsky_matvec_ratio = r = estimated_max_sht / estimated_max_fft
dl_fft *= flatsky_matvec_ratio


pick = (mask_gauss == 0)
n_mask = int(pick.sum())

def pad_vec(u):
    result = np.zeros(2 * nrings**2)
    result[pick] = u
    return result
    
def matvec_mask_basis(u):
    u_pad = np.zeros(2 * nrings**2)
    u_pad[pick] = u

    u_pad = flatsky_matvec(u_pad.reshape(nrings, 2 * nrings)).reshape(2 * nrings**2)
    return u_pad[pick]
        

dl_fft_inv = dl_fft.copy()
dl_fft_inv[dl_fft_inv != 0] = 1 / dl_fft_inv[dl_fft_inv != 0]

def precond_mask(u):
    u_pad = np.zeros(2 * nrings**2)
    u_pad[pick] = u
    u_pad = u_pad.reshape(nrings, 2 * nrings)
    u_pad = flatsky_analysis(u_pad)
    u_pad *= dl_fft_inv
    u_pad = flatsky_adjoint_analysis(u_pad)
    u_pad = u_pad.reshape(2 * nrings**2).real
    return u_pad[pick]


def solve_mask(b):
    # Then solve the 
    b_mask = sharp.sh_synthesis_gauss(lmax, b)[pick]
    
    solver = cg_generator(
        matvec_mask_basis,
        b_mask,
        M=precond_mask
        )

    bnorm = np.linalg.norm(b)
    resvec = []
    for i, (x, r, delta_new) in enumerate(solver):
    #for i in range(1000):
    #    r = b - matvec_mask_basis(x)
    #    x = x + precond(r)
        #errvec = x0 - x
        #err_its.append(errvec)
        #x_its.append(x)
        resvec.append(np.linalg.norm(r) / bnorm)
        if np.linalg.norm(r) < bnorm * 1e-6:
            break
        if i > 100:
            semilogy(resvec)
            draw()
            raise Exception('did not converge in 100 iterations!')
    print 'solved mask in ', i
    return sharp.sh_adjoint_synthesis_gauss(lmax, pad_vec(x))


    


if 0:
    clf()

    z_fft = np.zeros(2 * nrings**2)
    z_fft[nrings**2 + nrings] = 1

    z_fft = flatsky_matvec(z_fft.reshape(nrings, 2 * nrings))
    
    z_r = z.reshape(nrings, 2 * nrings)
    imshow((z_r - z_fft) / z_r.max(), interpolation='none')
    colorbar()
    draw()

    print 'ratio', estimated_max_sht / estimated_max_fft
    print 'maxerr', (z_r - z_fft).max() / z_r.max()
    #1/0

from cmbcr.precond_psuedoinv import lstadd, lstsub, lstmul


precond_1 = cmbcr.PsuedoInversePreconditioner(system)



def precond_both(b):
    x = precond_1.apply(b)

    if 1:
        x = lstadd(x, [solve_mask(b[0])])
        return x
    else:
        r = lstsub(b, system.matvec(x))
        x = lstadd(x, precond_mask(r))

        r = lstsub(b, system.matvec(x))
        x = lstadd(x, precond_1.apply(r))

        return x

    

precond = cmbcr.PsuedoInverseWithMaskPreconditioner(system, method='add1')

if 0:
    M = hammer(lambda x: system.stack(precond_both(system.unstack(x))), system.x_offsets[-1])
    #M = hammer(lambda x: system.stack(precond.apply(system.unstack(x))), system.x_offsets[-1])
    A = hammer(lambda x: system.stack(system.matvec(system.unstack(x))), system.x_offsets[-1])
    
    from scipy.linalg import eigvalsh, eigvals, eig

    
    #semilogy(np.abs(eigvalsh(A)), '-', label='A')
    #semilogy(np.abs(eigvalsh(M))[::-1], '-', label='M')

    w, vr = eig(np.dot(M, A))
    i = w.argsort()
    w = w[i]
    vr = vr[:,i]
    semilogy(w, '-o')
    1/0
    
    semilogy(sorted(np.abs(eigvals(np.dot(M, A)))), '-', label='MA^-1')
    #gca().set_ylim((1e-5, 1e8))
    legend()
    draw()
    1/0
#A = hammer(


errlst = []
x_its = []
err_its = []
norm0 = np.linalg.norm(x0_stacked)

err_its.append(x0)

solver = cg_generator(
    lambda x: system.stack(system.matvec(system.unstack(x))),
    system.stack(b),
    #x0=start_vec,
    M=lambda x: system.stack(precond_both(system.unstack(x))),
    )

for i, (x, r, delta_new) in enumerate(solver):
    x = system.unstack(x)

#for i in range(10):
#    r = lstsub(b, system.matvec(x))
#    x = lstadd(x, lstscale(1, precond_both(r)))

    errvec = lstsub(x0, x)
    err_its.append(errvec)
    x_its.append(x)
    
    errlst.append(np.linalg.norm(system.stack(errvec)) / norm0)

    print 'it', i
    if i > 20:
        break

#clf()
semilogy(errlst, '-o')
draw()