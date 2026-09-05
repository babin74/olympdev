template <int N>
struct Boom {
    Boom<N - 1> a, b;
    char c;
};

template <>
struct Boom<0> {
    char c;
};

Boom<24> bomb;

int main() {
    return bomb.c;
}
