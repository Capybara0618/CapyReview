import unittest

from capyreview.engineering_benchmark import (
    run_context_stress_benchmark,
    run_fault_injection_benchmark,
    run_fine_grained_recovery_benchmark,
)


class EngineeringBenchmarkTests(unittest.TestCase):
    def test_fault_injection_runs_50_cases_with_recovery_and_containment(self):
        report = run_fault_injection_benchmark()

        self.assertEqual(50, report["cases"])
        self.assertEqual(40, report["recoverable_cases"])
        self.assertEqual(10, report["expected_terminal_cases"])
        self.assertEqual(1.0, report["fault_recovery_rate"])
        self.assertEqual(1.0, report["fault_containment_rate"])
        self.assertEqual(1.0, report["state_consistency_rate"])
        self.assertEqual(1.0, report["trace_completeness_rate"])
        self.assertEqual(0, report["duplicate_side_effects"])
        self.assertEqual(50, len(report["case_results"]))
        self.assertEqual(
            {
                "transient_node_retry", "tool_argument_recovery",
                "checkpoint_resume", "duplicate_delivery",
                "budget_containment",
            },
            {item["scenario"] for item in report["case_results"]},
        )

    def test_context_stress_compacts_then_batches_without_losing_changed_lines(self):
        report = run_context_stress_benchmark()

        self.assertEqual(30, report["cases"])
        self.assertEqual(1.0, report["risk_evidence_retention_rate"])
        self.assertEqual(1.0, report["budget_compliance_rate"])
        self.assertEqual(1.0, report["contract_retention_rate"])
        self.assertEqual(1.0, report["compression_activation_rate"])
        self.assertEqual(1.0, report["batch_activation_rate"])
        self.assertEqual(1.0, report["changed_line_coverage_rate"])
        self.assertGreater(
            report["average_single_call_token_reduction_rate"], 0.7
        )
        self.assertGreater(report["average_cumulative_token_ratio"], 0.0)
        self.assertEqual(30, len(report["case_results"]))
        self.assertEqual(
            {"medium", "large", "xlarge"},
            {item["size_tier"] for item in report["case_results"]},
        )
        self.assertTrue(all(
            all(
                manifest["included"]["skills"] == 1
                and manifest["included"]["tools"] == 2
                for manifest in item["manifests"]
            )
            for item in report["case_results"]
        ))

    def test_fine_grained_recovery_covers_loop_reviewer_and_judge_boundaries(self):
        report = run_fine_grained_recovery_benchmark()

        self.assertEqual(30, report["cases"])
        self.assertEqual(1.0, report["recovery_rate"])
        self.assertEqual(1.0, report["state_consistency_rate"])
        self.assertEqual(1.0, report["trace_completeness_rate"])
        self.assertEqual(0, report["duplicate_llm_calls"])
        self.assertEqual(
            {"agent_loop_observation", "reviewer_final", "judge_decision"},
            {item["scenario"] for item in report["case_results"]},
        )


if __name__ == "__main__":
    unittest.main()
