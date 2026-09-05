import os

ISOLATE_CMD = os.getenv("OLYMPDEV_ISOLATE_CMD", "isolate")

# Contest / digest clamps. IsolateLimits and ProblemConfiguration share these.
MAX_TIME_LIMIT_MS = 300_000
MAX_WALL_TIME_MS = 600_000
MAX_MEMORY_LIMIT_MB = 4096
MAX_FILE_SIZE_LIMIT_KB = 256 * 1024
MAX_EXTRA_TIME_MS = 10_000
MAX_ASPACE_LIMIT_KB = MAX_MEMORY_LIMIT_MB * 1024
MAX_CG_MEMORY_LIMIT_KB = MAX_MEMORY_LIMIT_MB * 1024
MAX_TESTS_PER_PROBLEM = 1024
MAX_TEST_FILE_BYTES = 32 * 1024 * 1024
MAX_DICTIONARY_BYTES = 1_048_576

# isolate --init -q blocks,inodes (1 KiB blocks on ext). Dropped if the FS
# has no quota support.
DEFAULT_SANDBOX_QUOTA_BLOCKS = 256 * 1024
DEFAULT_SANDBOX_QUOTA_INODES = 4096


def check_limit_range(name: str, value: int, *, minimum: int, maximum: int) -> None:
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be in {minimum}..{maximum}, got {value}")
