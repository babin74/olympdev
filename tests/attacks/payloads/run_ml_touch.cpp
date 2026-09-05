#include <cstddef>

int main() {
    // Just above aplusb cg-mem (~128 MiB). Not 768 MiB: if cgroups slip on
    // WSL, a near-GB touch can OOM the whole utility VM.
    const std::size_t n = 192ull * 1024 * 1024;
    volatile char* p = new char[n];
    for (std::size_t i = 0; i < n; i += 4096) {
        p[i] = 1;
    }
    return p[0];
}
