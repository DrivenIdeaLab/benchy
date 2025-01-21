# ------------------------- Imports -------------------------
from typing import List, Optional
from datetime import datetime
from pathlib import Path
from modules.data_types import (
    ExecEvalBenchmarkFile,
    ExecEvalBenchmarkCompleteResult,
    ExeEvalBenchmarkOutputResult,
    ExecEvalBenchmarkModelReport,
    ExecEvalBenchmarkReport,
    ModelAlias,
    ExeEvalType,
    ModelProvider,
    BenchPromptResponse,
)
from modules.ollama_llm import bench_prompt
from modules.execution_evaluators import (
    execute_python_code,
    eval_result_compare,
)
from utils import parse_markdown_backticks
from modules import ollama_llm, anthropic_llm, deepseek_llm, gemini_llm

provider_delimiter = "~"


def parse_model_string(model: str) -> tuple[str, str]:
    """
    Parse model string into provider and model name.
    Format: "provider:model_name" or "model_name" (defaults to ollama)

    Raises:
        ValueError: If provider is not supported
    """
    if provider_delimiter not in model:
        # Default to ollama if no provider specified
        return "ollama", model

    provider, *model_parts = model.split(provider_delimiter)
    model_name = provider_delimiter.join(model_parts)

    # Validate provider
    supported_providers = [
        "ollama",
        "anthropic",
        "deepseek",
        "openai",
        "gemini",
        # "mlx",
        # "groq",
        # "fireworks",
    ]
    if provider not in supported_providers:
        raise ValueError(
            f"Unsupported provider: {provider}. "
            f"Supported providers are: {', '.join(supported_providers)}"
        )

    return provider, model_name


# ------------------------- File Operations -------------------------
def save_report_to_file(
    report: ExecEvalBenchmarkReport, output_dir: str = "reports"
) -> str:
    """Save benchmark report to file with standardized naming.

    Args:
        report: The benchmark report to save
        output_dir: Directory to save the report in

    Returns:
        Path to the saved report file
    """
    # Create output directory if it doesn't exist
    Path(output_dir).mkdir(exist_ok=True)

    # Generate filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_benchmark_name = report.benchmark_name.replace(" ", "_")
    report_filename = f"{output_dir}/{safe_benchmark_name}_{timestamp}.json"
    # Save report
    with open(report_filename, "w") as f:
        f.write(report.model_dump_json(indent=4))
    return report_filename


# ------------------------- Benchmark Execution -------------------------
def run_benchmark_for_model(
    model: str, benchmark_file: ExecEvalBenchmarkFile
) -> List[ExeEvalBenchmarkOutputResult]:
    results = []
    total_tests = len(benchmark_file.prompts)

    # Parse and validate the model string
    provider, model_name = parse_model_string(model)
    print(f"Running benchmark with provider: {provider}, model: {model_name}")

    for i, prompt_row in enumerate(benchmark_file.prompts, 1):
        print(f"  Running test {i}/{total_tests}...")

        # Replace dynamic variables in base prompt
        prompt = benchmark_file.base_prompt
        if prompt_row.dynamic_variables:
            for key, value in prompt_row.dynamic_variables.items():
                prompt = prompt.replace(f"{{{{{key}}}}}", str(value))

        # Get benchmark response based on provider
        if provider == "ollama":

            try:
                bench_response = ollama_llm.bench_prompt(prompt, model_name)
            except Exception as e:
                print(f"Error running Ollama model {model_name}: {str(e)}")
                bench_response = BenchPromptResponse(
                    response=f"Error: {str(e)}",
                    tokens_per_second=0.0,
                    provider="ollama",
                    total_duration_ms=0.0,
                    load_duration_ms=0.0,
                    errored=True,
                )
        elif provider == "anthropic":
            bench_response = anthropic_llm.bench_prompt(prompt, model_name)
        elif provider == "deepseek":
            bench_response = deepseek_llm.bench_prompt(prompt, model_name)
        elif provider == "openai":
            from modules.openai_llm import bench_prompt

            bench_response = bench_prompt(prompt, model_name)
        elif provider == "gemini":
            bench_response = gemini_llm.bench_prompt(prompt, model_name)
        else:
            raise ValueError(
                f"Unsupported model provider: {provider}. "
                f"Supported providers are: ollama, anthropic, deepseek, openai"
            )

        # Parse and execute the response
        cleaned_code = parse_markdown_backticks(bench_response.response)
        execution_result = ""
        expected_result = str(prompt_row.expectation).strip()

        try:
            if (
                benchmark_file.evaluator
                == ExeEvalType.execute_python_code_with_num_output
            ):
                execution_result = execute_python_code(cleaned_code)
                parsed_execution_result = str(execution_result).strip()
                correct = eval_result_compare(
                    benchmark_file.evaluator, expected_result, parsed_execution_result
                )
            elif (
                benchmark_file.evaluator
                == ExeEvalType.execute_python_code_with_string_output
            ):
                execution_result = execute_python_code(cleaned_code)
                correct = eval_result_compare(
                    benchmark_file.evaluator, expected_result, execution_result
                )
            elif benchmark_file.evaluator == ExeEvalType.raw_string_evaluator:
                execution_result = cleaned_code  # Use raw output
                correct = eval_result_compare(
                    benchmark_file.evaluator, expected_result, execution_result
                )
            else:
                raise ValueError(f"Unsupported evaluator: {benchmark_file.evaluator}")
        except Exception as e:
            print("Error executing code:", e)
            execution_result = str(e)
            correct = False

        # Store results
        results.append(
            ExeEvalBenchmarkOutputResult(
                input_prompt=prompt,
                prompt_response=bench_response,
                execution_result=str(execution_result),
                expected_result=str(expected_result),  # Add expected result
                model=model,
                correct=correct,
                index=i,  # Add the index
            )
        )
    return results


# ------------------------- Report Generation -------------------------
def generate_report(
    complete_result: ExecEvalBenchmarkCompleteResult,
) -> ExecEvalBenchmarkReport:
    model_reports = []

    # Group results by model
    model_results = {}
    for result in complete_result.results:
        if result.model not in model_results:
            model_results[result.model] = []
        model_results[result.model].append(result)

    # Create model reports
    for model, results in model_results.items():
        correct_count = sum(1 for r in results if r.correct)
        incorrect_count = len(results) - correct_count
        accuracy = correct_count / len(results)

        avg_tokens_per_second = sum(
            r.prompt_response.tokens_per_second for r in results
        ) / len(results)
        avg_total_duration = sum(
            r.prompt_response.total_duration_ms for r in results
        ) / len(results)
        avg_load_duration = sum(
            r.prompt_response.load_duration_ms for r in results
        ) / len(results)

        model_reports.append(
            ExecEvalBenchmarkModelReport(
                model=model,
                results=results,
                correct_count=correct_count,
                incorrect_count=incorrect_count,
                accuracy=accuracy,
                average_tokens_per_second=avg_tokens_per_second,
                average_total_duration_ms=avg_total_duration,
                average_load_duration_ms=avg_load_duration,
            )
        )

    # Calculate overall statistics
    overall_correct = sum(r.correct_count for r in model_reports)
    overall_incorrect = sum(r.incorrect_count for r in model_reports)
    overall_accuracy = overall_correct / (overall_correct + overall_incorrect)

    avg_tokens_per_second = sum(
        r.average_tokens_per_second for r in model_reports
    ) / len(model_reports)
    avg_total_duration = sum(r.average_total_duration_ms for r in model_reports) / len(
        model_reports
    )
    avg_load_duration = sum(r.average_load_duration_ms for r in model_reports) / len(
        model_reports
    )

    """Generate a comprehensive benchmark report from results.
    
    Args:
        complete_result: Completed benchmark results
        
    Returns:
        ExecEvalBenchmarkReport containing aggregated statistics
    """
    model_reports = []

    # Group results by model
    model_results = {}
    for result in complete_result.results:
        if result.model not in model_results:
            model_results[result.model] = []
        model_results[result.model].append(result)

    # Create model reports
    for model, results in model_results.items():
        correct_count = sum(1 for r in results if r.correct)
        incorrect_count = len(results) - correct_count
        accuracy = correct_count / len(results)

        avg_tokens_per_second = sum(
            r.prompt_response.tokens_per_second for r in results
        ) / len(results)
        avg_total_duration = sum(
            r.prompt_response.total_duration_ms for r in results
        ) / len(results)
        avg_load_duration = sum(
            r.prompt_response.load_duration_ms for r in results
        ) / len(results)

        model_reports.append(
            ExecEvalBenchmarkModelReport(
                model=model,
                results=results,
                correct_count=correct_count,
                incorrect_count=incorrect_count,
                accuracy=accuracy,
                average_tokens_per_second=avg_tokens_per_second,
                average_total_duration_ms=avg_total_duration,
                average_load_duration_ms=avg_load_duration,
            )
        )

    # Calculate overall statistics
    overall_correct = sum(r.correct_count for r in model_reports)
    overall_incorrect = sum(r.incorrect_count for r in model_reports)
    overall_accuracy = overall_correct / (overall_correct + overall_incorrect)

    avg_tokens_per_second = sum(
        r.average_tokens_per_second for r in model_reports
    ) / len(model_reports)
    avg_total_duration = sum(r.average_total_duration_ms for r in model_reports) / len(
        model_reports
    )
    avg_load_duration = sum(r.average_load_duration_ms for r in model_reports) / len(
        model_reports
    )

    return ExecEvalBenchmarkReport(
        benchmark_name=complete_result.benchmark_file.benchmark_name,
        purpose=complete_result.benchmark_file.purpose,
        models=model_reports,
        overall_correct_count=overall_correct,
        overall_incorrect_count=overall_incorrect,
        overall_accuracy=overall_accuracy,
        average_tokens_per_second=avg_tokens_per_second,
        average_total_duration_ms=avg_total_duration,
        average_load_duration_ms=avg_load_duration,
    )
