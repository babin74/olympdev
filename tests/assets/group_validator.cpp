#include "testlib.h"

// A validator that enforces a tighter bound on group 1 than on other
// groups, via testlib's own --group <value> convention:
// registerValidation(argc, argv) already parses it, exposed through
// validator.group() - see ProblemTest.valid_cmd.
int main(int argc, char *argv[]) {
    registerValidation(argc, argv);
    int limit = (validator.group() == "1") ? 10 : 1'000'000'000;
    inf.readInt(-limit, limit, "a");
    inf.readEoln();
    inf.readEof();
    return 0;
}
