#
#    GPT - Grid Python Toolkit
#    Copyright (C) 2020  Christoph Lehner (christoph.lehner@ur.de, https://github.com/lehner/gpt)
#                  2020  Daniel Richtmann (daniel.richtmann@ur.de)
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with this program; if not, write to the Free Software Foundation, Inc.,
#    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
import gpt, cgpt, sys, numpy


def create_links(A, fmat, basis, params):
    # NOTE: we expect the blocks in the basis vectors
    # to already be orthogonalized!
    # parameters
    hermitian = params["hermitian"]
    savelinks = params["savelinks"]
    uselut = params["uselut"]
    assert not (hermitian and not savelinks)

    # verbosity
    verbose = gpt.default.is_verbose("coarsen")

    # setup timings
    t = gpt.timer("coarsen")
    t("setup")

    # get grids
    f_grid = basis[0].grid
    c_grid = A[0].grid

    # directions/displacements we coarsen for
    dirs = [1, 2, 3, 4] if f_grid.nd == 5 else [0, 1, 2, 3]
    disp = +1
    dirdisps_full = list(zip(dirs * 2, [+1] * 4 + [-1] * 4))
    dirdisps_forward = list(zip(dirs, [disp] * 4))
    nhops = len(dirdisps_full)
    selflink = nhops

    # setup fields
    Mvr = [gpt.lattice(basis[0]) for i in range(nhops)]
    tmp = gpt.lattice(basis[0])
    oproj_scal = gpt.complex(c_grid)
    oproj_vec = gpt.vcomplex(c_grid, len(basis))
    selfproj = gpt.vcomplex(c_grid, len(basis))

    # setup masks
    onemask, blockevenmask, blockoddmask = (
        gpt.complex(f_grid),
        gpt.complex(f_grid),
        gpt.complex(f_grid),
    )
    dirmasks = [gpt.complex(f_grid) for p in range(nhops)]

    # auxilliary stuff needed for masks
    t("masks")
    onemask[:] = 1.0
    coor = gpt.coordinates(blockevenmask)
    block = numpy.array(f_grid.ldimensions) / numpy.array(c_grid.ldimensions)
    block_cb = coor[:, :] // block[:]

    # fill masks for sites within even/odd blocks
    gpt.make_mask(blockevenmask, numpy.sum(block_cb, axis=1) % 2 == 0)
    blockoddmask @= onemask - blockevenmask

    # fill masks for sites on borders of blocks
    dirmasks_forward_np = coor[:, :] % block[:] == block[:] - 1
    dirmasks_backward_np = coor[:, :] % block[:] == 0
    for mu in dirs:
        gpt.make_mask(dirmasks[mu], dirmasks_forward_np[:, mu])
        gpt.make_mask(dirmasks[mu + 4], dirmasks_backward_np[:, mu])

    # save applications of matrix and coarsening if possible
    dirdisps = dirdisps_forward if savelinks else dirdisps_full

    if uselut:
        # create lookup tables from masks
        t("luts")
        dirluts = [
            gpt.lookup_table(c_grid, dirmasks[p]) for p, (mu, fb) in enumerate(dirdisps)
        ]
        fulllut = gpt.lookup_table(c_grid, onemask)

    for i, vr in enumerate(basis):
        # apply directional hopping terms
        # this triggers len(dirdisps) comms -> TODO expose DhopdirAll from Grid
        # BUT problem with vector<Lattice<...>> in rhs
        t("apply_hop")
        [fmat.Mdir(Mvr[p], vr, mu, fb) for p, (mu, fb) in enumerate(dirdisps)]

        # coarsen directional terms + write to link
        for p, (mu, fb) in enumerate(dirdisps):
            if uselut:
                t("coarsen_hop")
                gpt.block.project_using_lut(oproj_vec, Mvr[p], basis, dirluts[p])

                t("copy_hop")
                A[p][:, :, :, :, :, i] = oproj_vec[:]
            else:
                for j, vl in enumerate(basis):
                    t("coarsen_hop")
                    gpt.block.masked_inner_product(oproj_scal, dirmasks[p], vl, Mvr[p])

                    t("copy_hop")
                    A[p][:, :, :, :, j, i] = oproj_scal[:]

        # fast diagonal term: apply full matrix to both block cbs separately and discard hops into other cb
        t("apply_self")
        tmp @= (
            blockevenmask * fmat * vr * blockevenmask
            + blockoddmask * fmat * vr * blockoddmask
        )

        # coarsen diagonal term
        t("coarsen_self")
        if uselut:
            gpt.block.project_using_lut(selfproj, tmp, basis, fulllut)
        else:
            gpt.block.project(selfproj, tmp, basis)

        # write to self link
        t("copy_self")
        A[selflink][:, :, :, :, :, i] = selfproj[:]

        if verbose:
            gpt.message("Coarsening of vector %d finished" % i)

    # communicate opposite links
    if savelinks:
        t("comm")
        comm_links(A, dirdisps_forward, hermitian)

    t()

    if verbose:
        gpt.message(t)


def comm_links(A, dirdisps_forward, hermitian):
    assert type(A) == list
    assert len(A) == 2 * len(dirdisps_forward) + 1
    for p, (mu, fb) in enumerate(dirdisps_forward):
        p_other = p + 4
        shift_fb = fb * -1
        Atmp = gpt.copy(A[p])
        if not hermitian:
            nbasis = A[0].otype.shape[0]
            assert nbasis % 2 == 0
            nb = nbasis // 2
            Atmp[:, :, :, :, 0:nb, nb:nbasis] *= -1.0  # upper right block
            Atmp[:, :, :, :, nb:nbasis, 0:nb] *= -1.0  # lower left block
        A[p_other] @= gpt.adj(gpt.cshift(Atmp, mu, shift_fb))


def recreate_links(A, fmat, basis):
    create_links(A, fmat, basis)
