// Validator: reads a test and fails unless it matches the statement exactly.
// Every test, generated or handwritten, goes through this - so the limits
// here are the ones the statement promises.
#include "testlib.h"

constexpr long long C = 1'000'000'000;

int main(int argc, char *argv[]) {
    registerValidation(argc, argv);
    inf.readLong(-C, C, "a");
    inf.readSpace();
    inf.readLong(-C, C, "b");
    inf.readEoln();
    inf.readEof();
}
