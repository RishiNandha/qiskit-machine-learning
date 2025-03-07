# This code is part of a Qiskit project.
#
# (C) Copyright IBM 2022, 2024.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Gradient of Sampler with Finite difference method."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from qiskit.circuit import Parameter, QuantumCircuit
from qiskit.providers import Options
from qiskit.quantum_info.operators.base_operator import BaseOperator
from qiskit.primitives.base import BaseEstimatorV2
from qiskit.primitives import BaseEstimator, BaseEstimatorV1
from qiskit.transpiler.passmanager import BasePassManager

from ..base.base_estimator_gradient import BaseEstimatorGradient
from ..base.estimator_gradient_result import EstimatorGradientResult

from ...exceptions import AlgorithmError


class SPSAEstimatorGradient(BaseEstimatorGradient):
    """
    Compute the gradients of the expectation value by the Simultaneous Perturbation Stochastic
    Approximation (SPSA) [1].

    **Reference:**
    [1] J. C. Spall, Adaptive stochastic approximation by the simultaneous perturbation method in
    IEEE Transactions on Automatic Control, vol. 45, no. 10, pp. 1839-1853, Oct 2020,
    `doi: 10.1109/TAC.2000.880982 <https://ieeexplore.ieee.org/document/880982>`_
    """

    # pylint: disable=too-many-positional-arguments
    def __init__(
        self,
        estimator: BaseEstimator,
        epsilon: float = 1e-6,
        batch_size: int = 1,
        seed: int | None = None,
        options: Options | None = None,
        pass_manager: BasePassManager | None = None,
    ):
        """
        Args:
            estimator: The estimator used to compute the gradients.
            epsilon: The offset size for the SPSA gradients.
            batch_size: The number of gradients to average.
            seed: The seed for a random perturbation vector.
            options: Primitive backend runtime options used for circuit execution.
                The order of priority is: options in ``run`` method > gradient's
                default options > primitive's default setting.
                Higher priority setting overrides lower priority setting.
            pass_manager: The pass manager to transpile the circuits if necessary.
                Defaults to ``None``, as some primitives do not need transpiled circuits.

        Raises:
            ValueError: If ``epsilon`` is not positive.
        """
        if epsilon <= 0:
            raise ValueError(f"epsilon ({epsilon}) should be positive.")
        self._epsilon = epsilon
        self._batch_size = batch_size
        self._seed = np.random.default_rng(seed)

        super().__init__(estimator, options=options, pass_manager=pass_manager)

    def _run(
        self,
        circuits: Sequence[QuantumCircuit],
        observables: Sequence[BaseOperator],
        parameter_values: Sequence[Sequence[float]] | np.ndarray,
        parameters: Sequence[Sequence[Parameter]],
        **options,
    ) -> EstimatorGradientResult:  # pragma: no cover
        """Compute the estimator gradients on the given circuits."""
        job_circuits, job_observables, job_param_values, metadata, offsets = [], [], [], [], []
        all_n = []
        for circuit, observable, parameter_values_, parameters_ in zip(
            circuits, observables, parameter_values, parameters
        ):
            # Indices of parameters to be differentiated.
            indices = [circuit.parameters.data.index(p) for p in parameters_]
            metadata.append({"parameters": parameters_})
            # Make random perturbation vectors.
            offset = [
                (-1) ** (self._seed.integers(0, 2, len(circuit.parameters)))
                for _ in range(self._batch_size)
            ]
            plus = [parameter_values_ + self._epsilon * offset_ for offset_ in offset]
            minus = [parameter_values_ - self._epsilon * offset_ for offset_ in offset]
            offsets.append(offset)

            # Combine inputs into a single job to reduce overhead.
            job_circuits.extend([circuit] * 2 * self._batch_size)
            job_observables.extend([observable] * 2 * self._batch_size)
            job_param_values.extend(plus + minus)
            all_n.append(2 * self._batch_size)
        if isinstance(self._estimator, BaseEstimatorV1):
            # Run the single job with all circuits.
            job = self._estimator.run(
                job_circuits,
                job_observables,
                job_param_values,
                **options,
            )
            try:
                results = job.result()
            except Exception as exc:
                raise AlgorithmError("Estimator job failed.") from exc
            # Compute the gradients.
            gradients = []
            partial_sum_n = 0
            for i, n in enumerate(all_n):
                result = results.values[partial_sum_n : partial_sum_n + n]
                partial_sum_n += n
                n = len(result) // 2
                diffs = (result[:n] - result[n:]) / (2 * self._epsilon)
                # Calculate the gradient for each batch.
                # Note that (``diff`` / ``offset``) is the gradient
                # since ``offset`` is a perturbation vector of 1s and -1s.
                batch_gradients = np.array(
                    [diff / offset for diff, offset in zip(diffs, offsets[i])]
                )
                # Take the average of the batch gradients.
                gradient = np.mean(batch_gradients, axis=0)
                indices = [circuits[i].parameters.data.index(p) for p in metadata[i]["parameters"]]
                gradients.append(gradient[indices])
            opt = self._get_local_options(options)
        elif isinstance(self._estimator, BaseEstimatorV2):
            if self._pass_manager is None:
                circs = job_circuits
                observables = job_observables
            else:
                circs = self._pass_manager.run(job_circuits)
                observables = [
                    op.apply_layout(circs[x].layout) for x, op in enumerate(job_observables)
                ]
            # Prepare circuit-observable-parameter tuples (PUBs)
            circuit_observable_params = []
            for pub in zip(circs, observables, job_param_values):
                circuit_observable_params.append(pub)

            # For BaseEstimatorV2, run the estimator using PUBs and specified precision
            job = self._estimator.run(circuit_observable_params)
            try:
                results = job.result()
            except Exception as exc:
                raise AlgorithmError("Estimator job failed.") from exc
            results = np.array([float(r.data.evs) for r in results])
            opt = Options(**options)

            # Compute the gradients.
            gradients = []
            partial_sum_n = 0
            for i, n in enumerate(all_n):
                result = results[partial_sum_n : partial_sum_n + n]
                partial_sum_n += n
                n = len(result) // 2
                diffs = (result[:n] - result[n:]) / (2 * self._epsilon)
                # Calculate the gradient for each batch.
                # Note that (``diff`` / ``offset``) is the gradient
                # since ``offset`` is a perturbation vector of 1s and -1s.
                batch_gradients = np.array(
                    [diff / offset for diff, offset in zip(diffs, offsets[i])]
                )
                # Take the average of the batch gradients.
                gradient = np.mean(batch_gradients, axis=0)
                indices = [circuits[i].parameters.data.index(p) for p in metadata[i]["parameters"]]
                gradients.append(gradient[indices])

        else:
            raise AlgorithmError(
                "The accepted estimators are BaseEstimatorV1 and BaseEstimatorV2; got "
                + f"{type(self._estimator)} instead. Note that BaseEstimatorV1 is deprecated in"
                + "Qiskit and removed in Qiskit IBM Runtime."
            )

        return EstimatorGradientResult(gradients=gradients, metadata=metadata, options=opt)
