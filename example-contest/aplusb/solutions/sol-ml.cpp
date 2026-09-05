#include <iostream>
#include <algorithm>
#include <vector>
#include <numeric>

constexpr int N = 1'000'000;

int main() {
    int a, b; std::cin >> a >> b;

    if (a < N && b < N && a > 0 && b > 0) {
        std::vector<int> v(N);
        std::iota(v.begin(), v.end(), 0);
        std::reverse(v.begin(), v.end());
        std::sort(v.begin(), v.end());
        std::cout << v[a + b] << std::endl;
    } else {
        std::cout << 0 << std::endl;
    }
}