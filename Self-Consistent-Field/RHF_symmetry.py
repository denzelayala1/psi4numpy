"""A restricted Hartree-Fock script using the Psi4NumPy formalism that
accounts for molecular symmetry using symmetrized orbitals.

References:
- Algorithm taken from [Szabo:1996], pp. 146
- Equations taken from [Szabo:1996]
"""

__authors__ = "Eric J. Berquist"
__credits__ = ["Eric J. Berquist", "Daniel G. A. Smith"]

__copyright__ = "(c) 2014-2018, The Psi4NumPy Developers"
__license__ = "BSD-3-Clause"
__date__ = "2018-12-26"

import time
import numpy as np
np.set_printoptions(precision=8, linewidth=200, suppress=True)
import psi4
from helper_HF import transform_aotoso, transform_sotoao

# Memory for Psi4 in GB
psi4.set_memory('500 MB')
psi4.core.set_output_file("output.dat", False)

# Memory for NumPy in GB
numpy_memory = 2

mol = psi4.geometry("""
O
H 1 1.1
H 1 1.1 2 104
""")

psi4.set_options({'basis': 'sto-3g',
                  'scf_type': 'direct',
                  'e_convergence': 1e-8})

# Set defaults
maxiter = 50
E_conv = 1.0E-8
D_conv = 1.0E-7

# Integral generation from Psi4's MintsHelper, which automatically
# performs symmetry adaptation.
wfn = psi4.core.Wavefunction.build(mol, psi4.core.get_global_option('BASIS'))
t = time.time()
mints = psi4.core.MintsHelper(wfn.basisset())

nirrep = wfn.nirrep()
# Water belongs to the C2v point group, which has four irreducable
# representations (irreps): A_1, A_2, B_1, and B_2.
assert nirrep == 4
nsopi = list(wfn.nsopi())

# Get nbf and ndocc for closed shell molecules
nbf = sum(nsopi)
ndocc = wfn.nalpha()

print('\nNumber of occupied orbitals: %d' % ndocc)
print('Number of basis functions: %d' % nbf)
print('Number of spin orbitals per irrep:', nsopi)

# Run a quick check to make sure everything will fit into memory
I_Size = (nbf**4) * 8.e-9
print("\nSize of the ERI tensor will be %4.2f GB." % I_Size)

# Estimate memory usage
memory_footprint = I_Size * 1.5
if I_Size > numpy_memory:
    psi4.core.clean()
    raise Exception("Estimated memory utilization (%4.2f GB) exceeds numpy_memory "
                    "limit of %4.2f GB." % (memory_footprint, numpy_memory))

def filter_empty_irrep(coll):
    """
    A matrix is returned for every irrep, even if the irrep is not present for
    the molecule; these must be filtered out to avoid problems with NumPy
    arrays that have a zero dimension.
    """
    return tuple(m for m in coll if all(m.shape))

# The convention will be to have quantities in the spin orbital (SO) basis
# ending with an underscore, each consisting of one matrix per irrep.
S_ = filter_empty_irrep(mints.so_overlap().to_array())
T_ = filter_empty_irrep(mints.so_kinetic().to_array())
V_ = filter_empty_irrep(mints.so_potential().to_array())

# The two-electron integrals are not blocked according to symmetry, so a
# transformation between the atomic orbital (AO) and SO bases will be required.
I_AO = np.asarray(mints.ao_eri())

# In order to convert from the C1 AO basis to the symmetrized SO basis, a set
# of matrices (one for each irrep with shape [nao, irrep_size]) is used to
# transform the dense AO representation into the block-diagonal SO
# representation. A SO-to-AO transformation matrix is obtained by transposing
# the corresponding AO-to-SO transformation matrix.
transformers_ = filter_empty_irrep(wfn.aotoso().to_array())

# At this point, all irreps that are not present (such as A_2 in water) should
# be filtered out.
assert len(S_) == len(T_) == len(V_) == len(transformers_)
nirrep = len(S_)
nsopi = [n for n in nsopi if n > 0]
print('Number of spin orbitals per irrep (with empty irreps removed):', nsopi)

print('\nTotal time taken for integrals: %.3f seconds.' % (time.time() - t))
t = time.time()

# Build H_core: [Szabo:1996] Eqn. 3.153, pp. 141
H_ = [T + V for (T, V) in zip(T_, V_)]

def build_orthogonalizer(S):
    """
    Form the orthogonalization matrix A = S^{-1/2} using pure NumPy.
    """
    # Application of a function to a matrix requires transforming to
    # the diagonal basis, applying the function to the diagonal form,
    # then backtransformation to the original basis.
    eigval, eigvec = np.linalg.eigh(S)
    eigval_diag = np.diag(eigval ** (-1./2))
    return eigvec.dot(eigval_diag).dot(eigvec.T)

A_ = [build_orthogonalizer(S) for S in S_]

def form_new_orbitals(A, F):
    Fp = A.dot(F).dot(A)        # Eqn. 3.177
    e, C2 = np.linalg.eigh(Fp)  # Solving Eqn. 1.178
    C = A.dot(C2)               # Back transform, Eqn. 3.174
    return C, e

# Calculate initial core guess: [Szabo:1996] pp. 145
C_ = []
e_ = []
for i in range(nirrep):
    C, e = form_new_orbitals(A_[i], H_[i])
    C_.append(C)
    e_.append(e)

# Occupations of each irrep are taken from the lowest eigenvalues (energies)
# of the guess coefficients.
energies_and_irreps = np.array(sorted(
    (energy, irrep)
    for (irrep, energies_irrep) in enumerate(e_)
    for energy in energies_irrep
))
lowest_occupied = energies_and_irreps[:ndocc, 1].astype(int).tolist()
ndoccpi = [lowest_occupied.count(i) for i in range(nirrep)]
print('Number of occupied spin orbitals per irrep:', ndoccpi)

# Form (occupied) density: [Szabo:1996] Eqn. 3.145, pp. 139
D_ = [np.einsum('mi,ni->mn', C_[i][:, :indocc], C_[i][:, :indocc])
      for i, indocc in enumerate(ndoccpi)]

print('\nTotal time taken for setup: %.3f seconds' % (time.time() - t))

print('\nStart SCF iterations:\n')
t = time.time()
E = 0.0
Enuc = mol.nuclear_repulsion_energy()
Eold = 0.0

E_ = np.array([(D * (H + H)).sum() for (D, H) in zip(D_, H_)])
E_1el = sum(E_) + Enuc
print('One-electron energy = %4.16f' % E_1el)

def form_new_density(A, F, indocc):
    C, _ = form_new_orbitals(A, F)
    Cocc = C[:, :indocc]
    return np.einsum('mi,ni->mn', Cocc, Cocc)

for SCF_ITER in range(1, maxiter + 1):

    # Perform the two-electron integral contraction with the density in the AO
    # basis.
    D_AO = transform_sotoao(D_, transformers_)
    J_ = transform_aotoso(np.einsum("mnls,ls->mn", I_AO, D_AO), transformers_)
    K_ = transform_aotoso(np.einsum("mlns,ls->mn", I_AO, D_AO), transformers_)
    F_ = [H + (2 * J) - K for H, J, K in zip(H_, J_, K_)]
    E_ = [np.einsum("mn,mn->", D, H + F) for D, H, F in zip(D_, H_, F_)]

    SCF_E = sum(E_) + Enuc

    print('SCF Iteration %3d: Energy = %4.16f   dE = % 1.5E' % (SCF_ITER, SCF_E, (SCF_E - Eold)))
    if abs(SCF_E - Eold) < E_conv:
        break

    Eold = SCF_E

    D_ = [form_new_density(A_[h], F_[h], indocc)
          for h, indocc in enumerate(ndoccpi)]

    if SCF_ITER == maxiter:
        psi4.core.clean()
        raise Exception("Maximum number of SCF cycles exceeded.")

print('Total time for SCF iterations: %.3f seconds \n' % (time.time() - t))

print('Final SCF energy: %.8f hartree' % SCF_E)
SCF_E_psi = psi4.energy('SCF')
psi4.compare_values(SCF_E_psi, SCF_E, 6, 'SCF Energy')
