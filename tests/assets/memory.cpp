#include <iostream>
#include <vector>
#include <cstdint>

int main() {
    const int n = 10'000'000;

    std::vector<uint32_t> a(n);
    for (int i = 1; i < n; ++i) {
        a[i] = (a[i - 1] ^ 274334u) * 13u;
    }

    uint32_t x = 0;
    for (int i = n - 2; i >= 0; --i) {
        a[i] ^= a[i + 1];
        x += a[i];
    }

    std::cout << x << std::endl;
}