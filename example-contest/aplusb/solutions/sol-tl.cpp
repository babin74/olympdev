#include <iostream>
#include <thread>
#include <chrono>

int main() {
    int a, b;
    std::cin >> a >> b;

    while (b > 0) {
        a++, b--;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    while (b < 0) {
        a--, b++;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    std::cout << a << std::endl;
}