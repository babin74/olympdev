// The reference solution: it defines the right answer for every test, so
// answers are never stored on disk. Keep it obviously correct rather than
// fast - `olympdev stress` compares other solutions against this one.
#include <iostream>

int main() {
    long long a, b;
    std::cin >> a >> b;
    std::cout << a + b << std::endl;
}
