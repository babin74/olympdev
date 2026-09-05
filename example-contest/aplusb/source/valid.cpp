#include "testlib.h"

constexpr int C = 1'000'000'000;

int main(int argc, char *argv[]) {
    registerValidation(argc, argv);
    int a = inf.readInt(-C, C, "a");
    inf.readSpace();
    int b = inf.readInt(-C, C, "b");
    inf.readEoln();
    inf.readEof();
    return 0;
}