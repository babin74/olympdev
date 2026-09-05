#include "testlib.h"

int main(int argc, char *argv[]) {
    registerGen(argc, argv, 1);
    const int c = opt<int>("c");
    int a = rnd.next(-c, c);
    int b = rnd.next(-c, c);
    println(a, b);
}