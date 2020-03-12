# -*- coding: utf-8 -*-
"""
Created on Wed Jul 15 08:34:51 2015

@author: Chris
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import curve_fit
from numpy.polynomial.chebyshev import chebval
import numba

from pyxe.fitting_functions import gaussian, lorentzian, psuedo_voigt
from pyxe.fitting_functions import strain_transformation


@numba.jit(nopython=True)
def pawley_sum(I, h, q, q0, fwhm, func_num):
    """ Computes diffraction profile for given set of Pawley parameters.

    Args:
        I (ndarray): Empty (zeros) intensity array of correct length
        h (ndarray): List of peak intensities
        q (ndarray): Reciprocal lattice
        q0 (ndarray): List of peak positions
        fwhm (ndarray): List of fwhm

    Returns:
        ndarray: Computed intensity profile
    """
    
    # print(sig)
    if func_num == 0:
        sig = fwhm / (2 * np.sqrt(2 * np.log(2)))
        for i in range(len(q0)):
            I = I + h[i] * np.exp(-(q - q0[i]) ** 2 / (2 * sig[i] ** 2))
        return I
    
    if func_num == 1:   
        sig = fwhm / 2
        for i in range(len(q0)):
            I = I + h[i] / (1.0 + ((q - q0[i]) / sig[i])**2)
            #I = I + h[i] * np.exp(-(q - q0[i]) ** 2 / (2 * sig[i] ** 2))
            #   p[0] + p[1] * np.exp(- (x - p[2])**2 / (2. * p[3]**2))
        return I


def pawley_hkl(detector, back, fw_order=None, func='gaussian'):
    """ Wrapper for Pawley fitting, allowing spec. of detector and background.

    Args:
        detector (pyxpb.peaks.Peak): pyxpb detector instance
        back (ndarray): Background intensity profile
        fw_order (int): Order of polynomial
    Returns:
        function: Pawley fitting function
        """

            
    def pawley(q, *p):
        
        if func == 'gaussian':
            func_num = 0
        else:
            func_num = 1 if func == 'lorentzian' else 2
            

        np_fw = fw_order + 1 if fw_order is not None else len(detector._fwhm)
        p0 = len(detector.hkl) + np_fw
        I = np.zeros_like(q)
        p_fw = p[p0 - np_fw: p0]
        # print(p_fw)

        for idx, material in enumerate(detector.materials):
            # Extract number of peaks and associated hkl values
            npeaks = len(detector.hkl[material])
            hkl = [[int(col) for col in row] for row in detector.hkl[material]]

            # Estimate of a and associated q0/d0 values
            a = p[idx]
            d0 = a / np.sqrt(np.sum(np.array(hkl)**2, axis=1))
            q0 = 2 * np.pi / d0
            q0 = q0[np.logical_and(q0 > np.min(q), q0 < np.max(q))]

            # Calculate FWHM and (associated) sigma/c value
            fwhm = np.expand_dims(detector.fwhm_q(q0, p_fw), 1)

            # Extract intensity values
            h = np.array(p[p0: p0 + npeaks])

            # print(fwhm)
            I = pawley_sum(I, h, q, q0, fwhm, func_num)
            p0 += npeaks

        return I + back
    return pawley


def extract_parameters(detector, q_lim, I_max=1, I_lim=None):
    """ Extract initial Pawley parameters from detector/setup.

    Args:
        detector (pyxpb.peaks.Peak): pyXpb detector instance
        q_lim (list, tuple): Limit q0 range for pawley fitting
        I_max (float): Multiplication factor for intensity
        I_lim (float): Relative minimum intensity limit

    Returns:
        list: Pawley parameter estimates
    """
    p = [detector.materials[mat]['a'] for mat in detector.materials]
    # print(p, type(p))
    p += detector._fwhm
    # print(p, type(p))
    for material in detector.materials:
        for idx, i in enumerate(detector.relative_heights()[material]):
            q0 = detector.q0[material][idx]
            if np.logical_and(q0 > q_lim[0], q0 < q_lim[1]):
                if I_lim is None or i > I_lim:
                    # print(p, type(p), i, I_max)
                    p = np.append(p, i * I_max)
    return list(p)


def q0_valid_range(detector, q_lim, I_lim=None):
    """ Finds valid q0 range (min, max) from detector/material/argument comb.

    Args:
        detector (pyxpb.peaks.Peak): pyXpb detector instance
        q_lim (list, tuple): Limit q0 range for pawley fitting
        I_max (float): Multiplication factor for intensity
        I_lim (float): Relative minimum intensity limit

    Returns:
        tuple: q_min, q_max
    """
    q0_valid = []
    for material in detector.materials:
        for idx, i in enumerate(detector.relative_heights()[material]):
            q0 = detector.q0[material][idx]
            if np.logical_and(q0 > q_lim[0], q0 < q_lim[1]):
                if I_lim is None or i > I_lim:
                    q0_valid.append(q0)
    return np.min(q0_valid), np.max(q0_valid)


def array_fit_pawley(q_array, I_array, detector, err_lim=1e-4,
                     q_lim=(2, None), progress=True, func='gaussian',
                     poisson=True):
    """ Pawley peak fit wrapper for ndarray of diffraction profiles/az slices.

    The peak fit is completed using a Gaussian profile assumption (lorentzian
    and psuedo-voigt to be implemented in the future). Specify an error limit
    as a threshold for valid peak fitting.

    Args:
        q_array (ndarray): 2d array containing q as a function of az slice idx
        I_array (ndarray): Nd array of intensity profiles wrt. posn/az_slice
        detector (pyxpb.peaks.Peak): pyxpb detector instance
        err_limit (float): Maximum error (in strain) for peak fit
        q_lim (list, tuple): Limit q0 range for pawley fitting
        progress (bool): Live progress bar
        poisson (bool): Poisson weighting

    Return:
        tuple: peaks, peaks_err, fwhm, fwhm_err (fwhm, fwhm_err = None, None)
    """
    nmat, nf = len(detector.materials), len(detector._fwhm)
    assert nmat > 0, "No materials have yet been specified."
    data = [np.nan * np.ones(I_array.shape[:-1]) for _ in range(4)]
    peaks, peaks_err, fwhm, fwhm_err = data
    slices = [i for i in range(q_array.shape[0])]

    err_exceed, run_error = 0, 0

    for az_idx in slices:

        # Load in detector calibrated q array and crop data
        q = q_array[az_idx]
        q_lim[0] = q_lim[0] if q_lim[0] is not None else np.min(q)
        q_lim[1] = q_lim[1] if q_lim[1] is not None else np.max(q)
        crop = np.logical_and(q > q_lim[0], q < q_lim[1])
        q = q[crop]
        q0_min, q0_max = q0_valid_range(detector, q_lim)

        # Only used to calc FWHM at approx. locations
        q0_range = np.linspace(q0_min, q0_max, 100)

        if detector._back.ndim == 2:
            background = chebval(q, detector._back[az_idx])
        else:
            background = chebval(q, detector._back)

        for position in np.ndindex(I_array.shape[:-2]):
            index = tuple(position) + (az_idx,)
            I = I_array[index][crop]
            p0 = extract_parameters(detector, q_lim, np.nanmax(I))

            # Fit peak across window
            try:
                pawley = pawley_hkl(detector, background, func=func)
                sig = 1 + I**0.5 if poisson else None
                coeff, var_mat = curve_fit(pawley, q, I, p0=p0, sigma=sig)
                perr = np.sqrt(np.diag(var_mat))
                peak, peak_err = coeff[0], perr[0]  # Single material
                pfw, pfw_err = coeff[nmat: nmat + nf], perr[nmat: nmat + nf]
                # print([pfw, q0_range[0], q0_range[-1]])
                fw = np.sum(np.polyval(pfw, q0_range)**0.5) / 100
                fw_err = np.sum(np.polyval(pfw_err, q0_range)**0.5) / 100
                # Check error and store
                if peak_err / peak > err_lim:
                    err_exceed += 1
                else:
                    peaks[index], peaks_err[index] = peak, peak_err
                    fwhm[index], fwhm_err[index] = fw, fw_err
            except RuntimeError:
                run_error += 1

            if progress:
                frac = (az_idx) / len(slices)
                prog = '\rProgress: [{0:20s}] {1:.0f}%'
                sys.stdout.write(prog.format('#' * int(20 * frac), 100 * frac))
                sys.stdout.flush()
    if progress:
        prog = '\rProgress: [{0:20s}] {1:.0f}%'
        sys.stdout.write(prog.format('#' * int(20 * 1), 100 * 1))
        sys.stdout.flush()
                
    print('\nTotal points: %i (%i az_angles x %i positions)'
          '\nPeak not found in %i position/detector combinations'
          '\nError limit exceeded (or pcov not estimated) %i times' %
          (peaks.size, peaks.shape[-1], peaks[..., 0].size,
           run_error, err_exceed))

    return peaks, peaks_err, fwhm, fwhm_err


def p0_approx(data, window, func='gaussian'):
    """ Esimates peak parameters for gauss/lorentz/psuedo-voigt peak fits.

    Args:
        data (tuple): q, I data arrays
        window (tuple): min, max edges of the search window
        func (str): Peak fitting function (gaussian, lorentzian, psuedo-voigt)

    Returns:
        tuple: Estimated peak parameters
    """
    x, y = data

    if x[0] > x[1]:
        x = x[::-1]
        y = y[::-1]

    peak_ind = np.searchsorted(x, window)
    q = x[peak_ind[0]:peak_ind[1]]
    I = y[peak_ind[0]:peak_ind[1]]
    max_index = np.argmax(I)
    hm = np.min(I) + (np.max(I) - np.min(I)) / 2
    
    stdev = q[max_index + np.argmin(I[max_index:] > hm)] - q[max_index]
    if stdev <= 0:
        stdev = 0.1
    p0 = [np.min(I), np.max(I) - np.min(I), q[max_index], stdev]
    p0.append(0) # linear background

    if func == 'psuedo_voigt':
        p0.append(0.5)
    return p0


def peak_fit(data, window, p0=None, func='gaussian', poisson=True):
    """ Peak fit for diffraction data across specified q window.

    The peak fitting is completed using either a Gaussian, Lorentzian or
    Psuedo-Voigt procedure. The initial estimate of parameter (p0) can be
    supplied or else computed.

    Args:
        data (tuple, list): q, I data arrays
        window (tuple, list): min, max edges of the search window
        p0 (tuple): Estimated curve paramaters
        func (str): Peak fitting function (gaussian, lorentzian, psuedo_voigt)

    Return:
        tuple: parameters, co-variance matrix (see scipy.optimize.curve_fit)
    """
    func_dict = {'gaussian': gaussian, 'lorentzian': lorentzian, 
                 'psuedo_voigt': psuedo_voigt}
    func_name = func
    func = func_dict[func.lower()]
    
    if data[0][0] > data[0][-1]:
        data[0] = data[0][::-1]
        data[1] = data[1][::-1]
        
    if p0 is None:
        p0 = p0_approx(data, window, func_name)
        
    peak_ind = np.searchsorted(data[0], window)
    x = data[0][peak_ind[0]:peak_ind[1]]
    I = data[1][peak_ind[0]:peak_ind[1]]

    sig = I**0.5 if poisson else None
    
    # Likely better than a fixed value for EDXRD versus. monochromatic
    # as min(EDXRD_I>0) = 1 whereas min(mono_I>0) << 1
    if sig is not None: 
        if np.max(sig) == 0:
            sig[sig == 0] = 1
        else:
            sig[sig == 0] = np.min(sig[sig > 0])
    
    # Weighted fit is done in the following manner (see scipy docs):
    # r = ydata - f(xdata, *popt)
    # chisq = sum((r / sigma) ** 2)
    # poisson weighting: chisq = sum((r / ydata**0.5) ** 2)
    
    return curve_fit(func, x, I, p0, sigma=sig)


def array_fit(q_array, I_array, window, func='gaussian',
              error_limit=1e-4, progress=True, poisson=True):
    """ Peak fit wrapper for ndarray of diffraction profiles/azimuhtal slices.

    The peak fitting is completed using either a Gaussian, Lorentzian or
    Psuedo-Voigt procedure. Specify an error limit as a threshold for valid
    peak fitting.

    Args:
        q_array (ndarray): 2d array containing q as a function of az slice idx
        I_array (ndarray): Nd array of intensity profiles wrt. posn/az_slice
        window (tuple): min, max edges of the search window
        func (str): Peak fitting function (gaussian, lorentzian, psuedo-voigt)
        error_limit (float): Maximum error (in strain) for peak fit
        progress (bool): Live progress bar

    Return:
        tuple: peaks, peaks_err, fwhm, fwhm_err
    """
    data = [np.nan * np.ones(I_array.shape[:-1]) for _ in range(4)]
    peaks, peaks_err, fwhm, fwhm_err = data 
    slices = [i for i in range(q_array.shape[0])]

    err_exceed, run_error = 0, 0

    for idx, az_idx in enumerate(slices):
        # Load in detector calibrated q array
        q = q_array[az_idx]
        for position in np.ndindex(I_array.shape[:-2]):
            index = tuple(position) + (az_idx,)
            I = I_array[index]
            p0 = p0_approx((q, I), window, func)
            
            # Fit peak across window
            try:
                coeff, var_matrix = peak_fit((q, I), window, p0, func, poisson)
                perr = np.sqrt(np.diag(var_matrix))                
                peak, peak_err = coeff[2], perr[2]
                fw, fw_err = coeff[3], perr[3]
                if func == 'gaussian':
                    fw, fw_err = fw * 2.35482, fw_err * 2.35482
                elif func == 'lorentzian':
                    fw, fw_err = fw * 2, fw_err * 2
                
                
                # Check error and store
                if np.abs(peak_err / peak) > error_limit:
                    err_exceed += 1
                else:
                    peaks[index], peaks_err[index] = peak, peak_err
                    fwhm[index], fwhm_err[index] = fw, fw_err
            except RuntimeError:
                run_error += 1
                
            if progress:
                percent = 100 * ((idx) / len(slices))
                sys.stdout.write('\rProgress: [{0:20s}] {1:.0f}%'.format('#' *
                                 int(percent / 5), percent))
                sys.stdout.flush()
    if progress:
        percent = 100 
        sys.stdout.write('\rProgress: [{0:20s}] {1:.0f}%'.format('#' *
                         int(percent / 5), percent))
        sys.stdout.flush()              
    print('\nTotal points: %i (%i az_angles x %i positions)'
          '\nPeak not found in %i position/detector combintions'
          '\nError limit exceeded (or pcov not estimated) %i times' % 
          (peaks.size, peaks.shape[-1], peaks[..., 0].size, 
           run_error, err_exceed))                  
    
    return peaks, peaks_err, fwhm, fwhm_err


def full_ring_fit(strain, phi):
    """ Computes strain tensor from phi v normal strain distribution.

    Fits the strain transformation equation to the calculated strain at each
    azimuthal location.

    Args:
        strain (ndarray): Nd strain array where final dimension is of len(phi)
        phi (ndarray): Azimuthal angle of each azimuthal slice (rad)

    Returns:
        tuple: Strain tensor (e_xx, e_yy, e_xy)
               Strain tensor error (e_xx_err, e_yy_err, e_xy_err)
               Strain tensor rmse (e_rmse)
    """
    strain_tensor = np.nan * np.ones(strain.shape[:-1] + (3,))
    strain_tensor_error = np.nan * np.ones(strain.shape[:-1] + (3,))
    strain_tensor_rmse = np.nan * np.ones(strain.shape[:-1] + (1,))

    error_count = 0
    for idx in np.ndindex(strain_tensor.shape[:-1]):
        data = strain[idx]
        not_nan = ~np.isnan(data)

        p0 = [np.nanmean(data), 3 * np.nanstd(data) / (2 ** 0.5), 0]
        try:
            popt, pcov = curve_fit(strain_transformation,
                             phi[not_nan], data[not_nan], p0)
            
            strain_tensor[idx] = popt
            strain_tensor_error[idx] = np.sqrt(np.diag(pcov))
            e_t = strain_transformation(phi, *popt)
            e = strain[idx]
            strain_tensor_rmse[idx] = np.nanmean((e_t - e)**2)**0.5
            
            
        except (TypeError, RuntimeError):
            error_count += 1
        #else:
        #    error_count += 1
    print('\nUnable to fit full ring at %i out of %i points'
          % (error_count, np.size(strain[..., 0])))

    return strain_tensor, strain_tensor_error, strain_tensor_rmse


#def mirror_data(phi, data):
#    """ Attempts to merge azimuthally distributed data across poles.
#
#    Only works in when there is an odd number of azimuthal slices.
#
#    Args:
#        phi (ndarray): Azimuthal slice positions (rad)
#        data (ndarray): Data to be azimuthally mirrored
#
#    Returns:
#        tuple: mphi, mdata - azimuthally merged data
#    """
#    mphi = phi[:int(phi[:].shape[0]/2)]
#    peak_shape = data.shape
#    phi_len = int(peak_shape[-2]/2)
#    new_shape = (peak_shape[:-2] + (phi_len, ) + peak_shape[-1:])
#    mdata = np.nan * np.zeros(new_shape)
#    for i in range(phi_len):
#        mdata[:, i] = (data[:, i] + data[:, i + new_shape[-2]]) / 2
#    return mphi, mdata
#
#
#def single_pawley(detector, q, I, back, p_fw=None, func='gaussian'):
#    """ Full pawley fitting for a specific q, I combination.
#
#    Option to use different initial parameters for FWHM fit. This allows a
#    lower order polynomial to be specified.
#
#    Args:
#        detector: pyxpb detector object
#        q (ndarray): Reciprocal lattice
#        I (ndarray): Intensity values
#        az_idx (int): Azimuthal slice index
#        p_fw (list, tuple): New initial estimate
#    """
#    nmat, nf = len(detector.materials), len(detector._fwhm)
#    background = chebval(q, back)
#    q_lim = [np.min(q), np.max(q)]
#    p0 = extract_parameters(detector, q_lim, np.nanmax(I))
#
#    if p_fw is not None:
#        p_fw0_idx = range(nmat, nmat + nf)
#        p0_new = [i for idx, i in enumerate(p0) if idx not in p_fw0_idx]
#
#        for idx, i in enumerate(p_fw):
#            p0_new.insert(nmat + idx, i)
#
#        nf = len(p_fw)
#    else:
#        p0_new = p0
#    pawley = pawley_hkl(detector, background, nf - 1, func=func)
#    return curve_fit(pawley, q, I, p0=p0_new)

#
#if __name__ == '__main__':
#    import os
#    from pyxe.energy_dispersive import EDI12
#    base = os.path.split(os.path.dirname(__file__))[0]
#    fpath_1 = os.path.join(base, r'pyxe/data/50418.nxs')
#    fpath_2 = os.path.join(base, r'pyxe/data/50414.nxs')
#    fine = EDI12(fpath_1)
#    fine.add_material('Fe')
#    fine.plot_intensity(pawley=True)
#    plt.show()
#    print(fine.detector.fwhm_param)