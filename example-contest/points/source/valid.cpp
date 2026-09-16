#include "testlib.h"

// Group 1 ("small") stays well within a 32-bit int's range even after
// summing two values; group 2 ("large") goes up to what a and b may be at
// all - exactly where sol-overflow's 32-bit sum breaks. registerValidation
// (argc, argv) parses --group <value> on its own; the default valid_cmd for
// a test with group_id under mode: "group-points" already supplies it (see
// ProblemTestingPipeline._resolve_valid_argv), so nothing in info.json needs
// to spell this out per test.
constexpr long long C_SMALL = 1'000LL;
constexpr long long C_LARGE = 3'000'000'000LL;

int main(int argc, char *argv[]) {
    registerValidation(argc, argv);
    long long limit = (validator.group() == "1") ? C_SMALL : C_LARGE;
    long long a = inf.readLong(-limit, limit, "a");
    inf.readSpace();
    long long b = inf.readLong(-limit, limit, "b");
    inf.readEoln();
    inf.readEof();
    return 0;
}
