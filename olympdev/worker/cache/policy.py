from enum import Enum


class CachePolicy(Enum):
    Never = "never"

    SolutionCompile = "solution_compile"
    ModelSolutionCompile = "model_solution_compile"
    CheckerCompile = "checker_compile"
    ValidatorCompile = "validator_compile"
    GeneratorCompile = "generator_compile"

    SolutionRun = "solution_run"
    ModelSolutionRun = "model_solution_run"
    Generator = "generator"
    Validator = "validator"
    Checker = "checker"
