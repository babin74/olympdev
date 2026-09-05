#include <fstream>
#include <iterator>
#include <string>

int main(int argc, char** argv) {
    if (argc < 4) {
        return 3;
    }
    std::ifstream output(argv[2], std::ios::binary);
    std::ifstream answer(argv[3], std::ios::binary);
    std::string got((std::istreambuf_iterator<char>(output)), {});
    std::string expected((std::istreambuf_iterator<char>(answer)), {});
    return got == expected ? 0 : 1;
}
