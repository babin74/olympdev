constexpr long long fib(int n) {
    return n < 2 ? n : fib(n - 1) + fib(n - 2);
}

constexpr auto k = fib(42);

int main() {
    return static_cast<int>(k % 1000000007);
}
