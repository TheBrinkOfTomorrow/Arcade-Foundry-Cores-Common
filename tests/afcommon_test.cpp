// Checks the archive readers against the fixtures tests/make_fixtures.py
// writes with Python's zipfile and tarfile. Usage: afcommon_test FIXTURE_DIR
#include "afcommon/archive.h"
#include "afcommon/tar.h"
#include "afcommon/tar_c.h"
#include "afcommon/zip_c.h"
#include "afcommon/zip.h"
#include "afcore.h"

#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <sstream>
#include <string>

namespace fs = std::filesystem;

static int failures = 0;

#define CHECK(cond)                                                                                                    \
    do {                                                                                                               \
        if (!(cond)) {                                                                                                 \
            std::fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #cond);                            \
            ++failures;                                                                                                \
        }                                                                                                              \
    } while (0)

static std::string slurp(const fs::path &p) {
    std::ifstream f(p, std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(f), {});
}

struct Expected {
    std::string name;
    uint64_t size;
    uint32_t crc;
};

static std::vector<Expected> manifest(const fs::path &dir) {
    std::vector<Expected> v;
    std::istringstream in(slurp(dir / "manifest.txt"));
    std::string name, crc;
    uint64_t size;
    while (in >> name >> size >> crc) v.push_back({name, size, uint32_t(std::stoul(crc, nullptr, 16))});
    return v;
}

static void zips(const fs::path &dir) {
    const auto want = manifest(dir);
    CHECK(want.size() == 5);
    for (const char *kind : {"stored.zip", "deflated.zip"}) {
        afc::Zip z((dir / kind).string());
        CHECK(z.entries().size() == want.size());
        for (const Expected &e : want) {
            CHECK(z.has(e.name));
            const auto data = z.read(e.name); // throws on a CRC mismatch
            CHECK(data.size() == e.size);
            CHECK(afc::crc32(data.data(), data.size()) == e.crc);
        }
        // The same through open_archive, which picks the reader by signature.
        auto a = afc::open_archive((dir / kind).string());
        CHECK(a->entries().size() == want.size());
        CHECK(a->read("text.bin").size() == a->entries().at("text.bin").size);
    }
    {
        afc::Zip z((dir / "fixed.zip").string());
        const auto d = z.read("short.bin");
        CHECK(std::string(d.begin(), d.end()) == "abcabcabcabc hello hello");
    }
    bool threw = false;
    try {
        afc::Zip((dir / "badcrc.zip").string()).read("text.bin");
    } catch (const afc::ZipError &) {
        threw = true;
    }
    CHECK(threw);
    // The C interface: a member by name in any case, cut to the buffer, and the errors.
    {
        char err[256] = "", buf[4];
        afc_zip *z = afc_zip_open((dir / "fixed.zip").string().c_str(), err, sizeof err);
        CHECK(z != nullptr);
        if (z) {
            const auto whole = afc::Zip((dir / "fixed.zip").string()).read("short.bin");
            CHECK(afc_zip_read(z, "SHORT.BIN", buf, sizeof buf, err, sizeof err) == (long long)whole.size());
            CHECK(std::memcmp(buf, whole.data(), sizeof buf) == 0);
            CHECK(afc_zip_read(z, "nothere.bin", buf, sizeof buf, err, sizeof err) == -1);
            CHECK(std::string(err).find("missing") != std::string::npos);
            afc_zip_close(z);
        }
        CHECK(afc_zip_open((dir / "nothere.zip").string().c_str(), err, sizeof err) == nullptr);
        CHECK(std::string(err).find("cannot open") != std::string::npos);
        afc_zip_close(nullptr);
    }
    // A directory claiming too much is refused before anything is allocated.
    for (const char *big : {"toobig.zip", "ratio.zip"}) {
        std::string why;
        try {
            afc::Zip((dir / big).string()).read("zeros.bin");
        } catch (const afc::ZipError &e) {
            why = e.what();
        }
        CHECK(why.find("too big") != std::string::npos);
    }
    threw = false;
    try {
        afc::open_archive((dir / "game-gnu.tar").string());
    } catch (const afc::ZipError &) {
        threw = true;
    }
    CHECK(threw);
}

static void sevenzips(const fs::path &dir) {
    const auto want = manifest(dir);
    for (const char *kind : {"lzma2.7z", "lzma.7z"}) {
        if (!fs::exists(dir / kind)) {
#ifdef AFC_HAVE_7Z
            std::fprintf(stderr, "%s missing (py7zr: tools/afic-venv/setup.sh): 7z not checked\n", kind);
            ++failures;
#endif
            continue;
        }
        bool threw = false;
        try {
            auto a = afc::open_archive((dir / kind).string());
            CHECK(a->entries().size() == want.size());
            for (const Expected &e : want) {
                CHECK(a->entries().count(e.name) == 1);
                const auto data = a->read(e.name); // the solid block decoded once, CRC checked
                CHECK(data.size() == e.size);
                CHECK(afc::crc32(data.data(), data.size()) == e.crc);
            }
        } catch (const afc::ZipError &e) {
            threw = true;
            std::printf("%s: %s\n", kind, e.what());
        }
#ifdef AFC_HAVE_7Z
        CHECK(!threw);
#else
        CHECK(threw); // "7z support was not built"
#endif
    }
}

static const afc::TarOptions kGame{{"hotd4a", "HOTD4"}, "disk0"};

// Feeds the tar in `chunk`-byte pieces; returns "" or the error.
static std::string unpack(const fs::path &tar, const fs::path &dst, size_t chunk) {
    const std::string bytes = slurp(tar);
    try {
        afc::TarUnpacker u(dst, kGame);
        for (size_t at = 0; at < bytes.size(); at += chunk)
            u.feed(reinterpret_cast<const uint8_t *>(bytes.data()) + at, std::min(chunk, bytes.size() - at));
        u.finish();
    } catch (const afc::TarError &e) {
        return e.what();
    }
    return "";
}

static void check_game(const fs::path &g) {
    CHECK(slurp(g / "disk0/elf/hod4").size() == 2004);
    CHECK(slurp(g / "disk0/elf/hod4").compare(0, 4, "\x7f" "ELF") == 0);
    CHECK(slurp(g / "disk0/hard_copy") == slurp(g / "disk0/elf/hod4"));
    CHECK(fs::is_symlink(g / "disk0/link_to_elf"));
    CHECK(fs::read_symlink(g / "disk0/link_to_elf") == "elf/hod4");
    CHECK(slurp(g / "disk0/readonly.bin") == "ro");
    CHECK((fs::status(g / "disk0/readonly.bin").permissions() & fs::perms::owner_write) != fs::perms::none);
    CHECK((fs::status(g / "disk0/elf/hod4").permissions() & fs::perms::owner_exec) != fs::perms::none);
    fs::path deep = g / "disk0";
    for (int i = 0; i < 6; ++i) deep /= "a_rather_long_folder_name";
    CHECK(slurp(deep / "deep.txt") == "deep");
    CHECK(!fs::exists(g / "disk0/._hod4"));
    CHECK(!fs::exists(g / "disk0/.DS_Store"));
    CHECK(!fs::exists(g / "__MACOSX"));
    CHECK(!fs::exists(g / "hotd4a"));
    CHECK(!fs::exists(g.string() + ".unpacking"));
}

static void tars(const fs::path &dir, const fs::path &work) {
    for (const char *kind : {"game-gnu.tar", "game-pax.tar"}) {
        for (size_t chunk : {size_t(1) << 20, size_t(7), size_t(512)}) {
            const fs::path dst = work / "hotd4a";
            fs::create_directories(dst / "stale");
            CHECK(unpack(dir / kind, dst, chunk).empty());
            CHECK(!fs::exists(dst / "stale")); // replaced whole
            check_game(dst);
        }
    }
    {
        const fs::path dst = work / "flat";
        CHECK(afc::unpack_tar_file(dir / "game-flat.tar", dst, kGame).empty());
        CHECK(slurp(dst / "disk0/file.bin") == "flat");
    }
    for (const char *bad : {"escape-dotdot.tar", "escape-abs.tar", "escape-symlink.tar"}) {
        const fs::path dst = work / "bad";
        const std::string err = unpack(dir / bad, dst, 1 << 20);
        CHECK(err.find("outside the game folder") != std::string::npos);
        CHECK(!fs::exists(dst));
        CHECK(!fs::exists(work / "bad.unpacking"));
    }
    CHECK(!fs::exists(work / "evil"));
    CHECK(unpack(dir / "no-disk0.tar", work / "nd", 1 << 20).find("no disk0") != std::string::npos);
    CHECK(unpack(dir / "truncated.tar", work / "tr", 100).find("ended early") != std::string::npos);
    CHECK(unpack(dir / "not-a-tar.tar", work / "nt", 100).find("not a tar") != std::string::npos);
    CHECK(unpack(dir / "huge-pax.tar", work / "hp", 1 << 20).find("too big") != std::string::npos);
    for (const char *n : {"nd", "tr", "nt", "hp"}) CHECK(!fs::exists(work / n) && !fs::exists(work / (std::string(n) + ".unpacking")));
    // With a progress callback, through the file helper.
    double last = 0;
    CHECK(afc::unpack_tar_file(dir / "game-gnu.tar", work / "p", kGame, [&](double f) { last = f; }).empty());
    CHECK(last > 0.9);
    CHECK(afc::unpack_tar_file(dir / "truncated.tar", work / "q", kGame).rfind("truncated.tar: ", 0) == 0);

    // The C interface the Apple front end uses.
    const std::string bytes = slurp(dir / "game-pax.tar");
    const char *names[] = {"hotd4a"};
    afc_tar *t = afc_tar_begin((work / "c").string().c_str(), names, 1, "disk0");
    CHECK(afc_tar_feed(t, bytes.data(), bytes.size()) == 0);
    CHECK(afc_tar_finish(t) == 0);
    afc_tar_free(t);
    check_game(work / "c");
    t = afc_tar_begin((work / "c2").string().c_str(), names, 1, "disk0");
    const std::string cut = slurp(dir / "truncated.tar");
    CHECK(afc_tar_feed(t, cut.data(), cut.size()) == 0);
    CHECK(afc_tar_finish(t) == -1);
    CHECK(std::string(afc_tar_error(t)).find("ended early") != std::string::npos);
    afc_tar_free(t);
    CHECK(!fs::exists(work / "c2.unpacking"));
}

int main(int argc, char **argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: afcommon_test FIXTURE_DIR\n");
        return 2;
    }
    const fs::path dir = argv[1], work = dir / "work";
    fs::remove_all(work);
    fs::create_directories(work);
    CHECK(AFCORE_API_VERSION >> 16 >= 5);
    try {
        zips(dir);
        sevenzips(dir);
        tars(dir, work);
    } catch (const std::exception &e) {
        std::fprintf(stderr, "unexpected exception: %s\n", e.what());
        ++failures;
    }
    std::printf("%s (%d failure%s)\n", failures ? "FAILED" : "ok", failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
