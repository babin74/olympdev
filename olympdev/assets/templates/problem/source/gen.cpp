// Test generator. Arguments come from a test's `gen_cmd` in info.json
// ("gen -c=1000" passes -c=1000 here), and the same argv always produces the
// same test: testlib seeds itself from it.
#include "testlib.h"

int main(int argc, char *argv[]) {
    registerGen(argc, argv, 1);
    const long long c = opt<long long>("c");
    println(rnd.next(-c, c), rnd.next(-c, c));
}
