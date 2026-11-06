// A larger Win64 program for the 64-bit recompiler: C++ with the static
// libstdc++ (containers, strings, iostreams, virtual dispatch, sorting).
#include <windows.h>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <cmath>
#include <string>
#include <vector>
#include <map>
#include <algorithm>
#include <sstream>
#include <memory>

struct Shape { virtual ~Shape() {} virtual double area() const = 0; virtual const char *name() const = 0; };
struct Circle : Shape { double r; Circle(double r) : r(r) {} double area() const override { return 3.14159265358979 * r * r; } const char *name() const override { return "circle"; } };
struct Rect : Shape { double w, h; Rect(double w, double h) : w(w), h(h) {} double area() const override { return w * h; } const char *name() const override { return "rect"; } };

static uint64_t lcg(uint64_t &s) { s = s * 6364136223846793005ULL + 1442695040888963407ULL; return s >> 33; }

__attribute__((noinline)) static long double ld_sum(const std::vector<double> &v) {
    long double s = 0;
    for (double d : v) s += (long double)d / 3.0L;
    return s;
}

__attribute__((noinline)) static int dispatch(int op, int a, int b) {
    switch (op) {
    case 0: return a + b; case 1: return a - b; case 2: return a * b; case 3: return b ? a / b : 0;
    case 4: return a % (b ? b : 1); case 5: return a & b; case 6: return a | b; case 7: return a ^ b;
    case 8: return a << (b & 31); case 9: return a >> (b & 31); case 10: return (int)((unsigned)a >> (b & 31));
    case 11: return std::max(a, b); case 12: return std::min(a, b); default: return -1;
    }
}

int main(int argc, char **argv) {
    uint64_t seed = 12345 + argc;
    std::vector<std::unique_ptr<Shape>> shapes;
    for (int i = 0; i < 50; i++) {
        if (lcg(seed) & 1) shapes.emplace_back(new Circle((lcg(seed) % 100) / 10.0));
        else shapes.emplace_back(new Rect((lcg(seed) % 50) / 5.0, (lcg(seed) % 50) / 5.0));
    }
    std::sort(shapes.begin(), shapes.end(), [](const std::unique_ptr<Shape> &a, const std::unique_ptr<Shape> &b) { return a->area() < b->area(); });
    std::map<std::string, int> counts;
    double total = 0;
    for (auto &s : shapes) { counts[s->name()]++; total += s->area(); }
    std::ostringstream os;
    for (auto &kv : counts) os << kv.first << "=" << kv.second << " ";
    os << "total=" << (long long)(total * 1000);
    std::vector<double> v;
    for (int i = 0; i < 1000; i++) v.push_back((double)(lcg(seed) % 10000) / 7.0);
    long double ls = ld_sum(v);
    int acc = 0;
    for (int i = 0; i < 10000; i++) acc = dispatch((int)(lcg(seed) % 14), acc, (int)(lcg(seed) % 1000) - 500);
    std::string line = os.str();
    printf("%s\n", line.c_str());
    printf("ld %.4Lf acc %d sqrt %.6f\n", ls, acc, std::sqrt((double)v.size() + argc));
    std::string big(100000, 'x');
    for (size_t i = 0; i < big.size(); i += 7) big[i] = 'y';
    printf("count %zu hash %08x\n", (size_t)std::count(big.begin(), big.end(), 'y'), (unsigned)std::hash<std::string>()(big));
    return 0;
}
