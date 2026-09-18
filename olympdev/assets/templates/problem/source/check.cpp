// Checker: compares a participant's output against the reference answer.
// A plain byte comparison would reject a trailing newline, so compare values
// and use seekEof(), which skips whitespace, to insist there is nothing more.
#include "testlib.h"

int main(int argc, char *argv[]) {
    setName("compare a single signed integer");
    registerTestlibCmd(argc, argv);

    long long expected = ans.readLong();
    long long found = ouf.readLong();
    if (expected != found)
        quitf(_wa, "expected %lld, found %lld", expected, found);
    if (!ouf.seekEof())
        quitf(_pe, "extra output after the answer");
    quitf(_ok, "%lld", found);
}
