#include "testlib.h"

// Random a, b within one group's range - group 1 stays small enough that
// int32 addition never overflows, group 2 goes up to the validator's own
// bound (see source/valid.cpp), which does overflow a 32-bit sum - see
// solutions/sol-overflow.cpp.
int main(int argc, char *argv[]) {
    registerGen(argc, argv, 1);
    int group = opt<int>("group", 1);
    long long limit = (group == 1) ? 1'000 : 3'000'000'000LL;
    long long a = rnd.next(-limit, limit);
    long long b = rnd.next(-limit, limit);
    println(a, b);
}
