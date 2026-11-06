#include "afcommon/tar.h"
#include "afcommon/tar_c.h"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <cstring>
#include <memory>

namespace fs = std::filesystem;

namespace afc {

namespace {

// Octal, with leading spaces or NULs; GNU's base-256 (top bit set) for sizes over 8 GB.
uint64_t number(const uint8_t *b, int n) {
    uint64_t v = 0;
    if (b[0] & 0x80) {
        v = b[0] & 0x7f;
        for (int i = 1; i < n; ++i) v = v << 8 | b[i];
        return v;
    }
    for (int i = 0; i < n; ++i) {
        if (b[i] == 0 || b[i] == ' ') {
            if (v) break;
            continue;
        }
        if (b[i] < '0' || b[i] > '7') break;
        v = v * 8 + uint64_t(b[i] - '0');
    }
    return v;
}

std::string field(const uint8_t *b, size_t n) {
    return std::string(reinterpret_cast<const char *>(b), strnlen(reinterpret_cast<const char *>(b), n));
}

// macOS's own files, not the game's: AppleDouble "._" files (which macOS tar
// and non-Mac volumes add beside each file), __MACOSX and .DS_Store.
bool is_metadata(const std::string &part) {
    return part.rfind("._", 0) == 0 || part == "__MACOSX" || part == ".DS_Store";
}

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return char(std::tolower(c)); });
    return s;
}

} // namespace

TarUnpacker::TarUnpacker(fs::path dst, TarOptions options) : dst_(std::move(dst)), opt_(std::move(options)) {
    for (std::string &n : opt_.top_names) n = lower(n);
    staging_ = dst_;
    staging_ += ".unpacking";
    std::error_code ec;
    fs::remove_all(staging_, ec);
    fs::create_directories(staging_, ec);
    if (ec) throw TarError(staging_.string() + ": " + ec.message());
    base_ = fs::weakly_canonical(staging_, ec).string();
}

TarUnpacker::~TarUnpacker() {
    if (!done_) cancel();
}

void TarUnpacker::close_file() {
    if (file_) std::fclose(file_);
    file_ = nullptr;
}

void TarUnpacker::cancel() {
    close_file();
    std::error_code ec;
    fs::remove_all(staging_, ec);
    done_ = true;
}

void TarUnpacker::feed(const uint8_t *data, size_t n) {
    if (failed_) throw TarError("unpacking already failed");
    if (done_) throw TarError("unpacking already finished");
    fed_ += n;
    try {
        size_t at = 0;
        while (at < n && !ended_) {
            if (remaining_ > 0) {
                const size_t k = size_t(std::min<uint64_t>(n - at, remaining_));
                if (is_collecting_) {
                    collecting_.append(reinterpret_cast<const char *>(data + at), k);
                } else if (file_ && std::fwrite(data + at, 1, k, file_) != k) {
                    throw TarError("could not write a file (disk full?)");
                }
                remaining_ -= k;
                at += k;
                if (remaining_ == 0) end_entry();
            } else if (padding_ > 0) {
                const size_t k = size_t(std::min<uint64_t>(n - at, padding_));
                padding_ -= k;
                at += k;
            } else {
                const size_t k = std::min(n - at, sizeof header_ - header_len_);
                std::memcpy(header_ + header_len_, data + at, k);
                header_len_ += k;
                at += k;
                if (header_len_ == sizeof header_) {
                    header_len_ = 0;
                    start_entry();
                }
            }
        }
    } catch (...) {
        failed_ = true;
        close_file();
        throw;
    }
}

void TarUnpacker::start_entry() {
    const uint8_t *h = header_;
    if (std::all_of(h, h + 512, [](uint8_t c) { return c == 0; })) { // the end blocks
        ended_ = true;
        return;
    }
    unsigned sum = 0;
    for (int i = 0; i < 512; ++i) sum += (i >= 148 && i < 156) ? ' ' : h[i];
    if (sum != number(h + 148, 8)) throw TarError("not a tar file");
    const char type = char(h[156]);
    uint64_t size = number(h + 124, 12);
    if (pax_size_ != UINT64_MAX && type != 'x' && type != 'g') size = pax_size_;
    remaining_ = size;
    padding_ = (512 - size % 512) % 512;
    if (type == 'L' || type == 'K' || type == 'x' || type == 'g') { // 'g', a global pax header: read and ignored
        if (size > kMaxTarHeaderBytes) throw TarError("a long name or pax header is too big (damaged tar?)");
        collecting_.clear();
        collecting_type_ = type;
        is_collecting_ = true;
    } else {
        std::string name = long_name_;
        if (name.empty()) {
            name = field(h, 100);
            const std::string prefix = std::memcmp(h + 257, "ustar", 5) == 0 ? field(h + 345, 155) : "";
            if (!prefix.empty()) name = prefix + "/" + name;
        }
        const std::string link = long_link_.empty() ? field(h + 157, 100) : long_link_;
        long_name_.clear();
        long_link_.clear();
        pax_size_ = UINT64_MAX;
        create(name, type, link, unsigned(number(h + 100, 8)) & 0777);
    }
    if (remaining_ == 0) end_entry();
}

void TarUnpacker::end_entry() {
    if (!is_collecting_) {
        close_file();
        return;
    }
    is_collecting_ = false;
    const std::string text = collecting_.substr(0, strnlen(collecting_.c_str(), collecting_.size()));
    if (collecting_type_ == 'L') long_name_ = text;
    if (collecting_type_ == 'K') long_link_ = text;
    if (collecting_type_ == 'x') { // "<length> <key>=<value>\n" records
        size_t at = 0;
        while (at < collecting_.size()) {
            const size_t nl = collecting_.find('\n', at), space = collecting_.find(' ', at);
            if (nl == std::string::npos || space == std::string::npos || space > nl) break;
            const std::string rec = collecting_.substr(space + 1, nl - space - 1);
            const size_t eq = rec.find('=');
            if (eq != std::string::npos) {
                const std::string key = rec.substr(0, eq), value = rec.substr(eq + 1);
                if (key == "path") long_name_ = value;
                if (key == "linkpath") long_link_ = value;
                if (key == "size") pax_size_ = std::strtoull(value.c_str(), nullptr, 10);
            }
            at = nl + 1;
        }
    }
    collecting_.clear();
}

// The entry's place in the staging folder: "./" and a top folder named in
// top_names dropped. Empty when it would leave the folder.
fs::path TarUnpacker::inside(const std::string &name, bool &top, bool &metadata) const {
    std::vector<std::string> parts;
    size_t at = 0;
    while (at <= name.size()) {
        size_t slash = name.find('/', at);
        if (slash == std::string::npos) slash = name.size();
        std::string p = name.substr(at, slash - at);
        if (!p.empty() && p != ".") parts.push_back(p);
        at = slash + 1;
    }
    top = false;
    metadata = std::any_of(parts.begin(), parts.end(), is_metadata);
    if (!parts.empty() && std::find(opt_.top_names.begin(), opt_.top_names.end(), lower(parts[0])) != opt_.top_names.end())
        parts.erase(parts.begin());
    if (!name.empty() && name[0] == '/') return {};
    if (std::find(parts.begin(), parts.end(), "..") != parts.end()) return {};
    if (parts.empty()) {
        top = true;
        return staging_;
    }
    fs::path p = staging_;
    for (const std::string &part : parts) p /= part;
    return p;
}

void TarUnpacker::create(const std::string &name, char type, const std::string &link, unsigned mode) {
    bool top = false, metadata = false;
    const fs::path out = inside(name, top, metadata);
    if (out.empty()) throw TarError("a file outside the game folder: " + name);
    if (top || metadata) return; // the top folder itself, or Finder's files: the entry's data is skipped
    std::error_code ec;
    fs::create_directories(out.parent_path(), ec);
    // A symbolic link earlier in the tar must not lead a file out of the game folder.
    const std::string parent = fs::weakly_canonical(out.parent_path(), ec).string();
    if (parent.compare(0, base_.size(), base_) != 0 || (parent.size() > base_.size() && parent[base_.size()] != '/' &&
                                                        parent[base_.size()] != fs::path::preferred_separator))
        throw TarError("a file outside the game folder: " + name);
    switch (type) {
    case '5':
        fs::create_directories(out, ec);
        break;
    case '2':
        fs::remove(out, ec);
        fs::create_symlink(link, out, ec);
        if (ec) throw TarError(out.string() + ": " + ec.message());
        break;
    case '1': { // a hard link: a copy of the file it names
        bool t = false, m = false;
        const fs::path target = inside(link, t, m);
        if (target.empty() || t) throw TarError("a bad link: " + link);
        fs::remove(out, ec);
        fs::copy_file(target, out, ec);
        if (ec) throw TarError(out.string() + ": " + ec.message());
        break;
    }
    case '0':
    case '\0':
    case '7': {
        fs::remove(out, ec);
        file_ = std::fopen(out.string().c_str(), "wb");
        if (!file_) throw TarError(out.string() + ": " + std::strerror(errno));
        // Always writable by us: a read-only file (0444) is still replaced next time.
        fs::permissions(out, fs::perms((mode ? mode : 0644) | 0200), ec);
        break;
    }
    default: // devices and FIFOs: not part of a game
        break;
    }
}

void TarUnpacker::finish() {
    if (failed_ || done_) throw TarError("unpacking already ended");
    close_file();
    std::error_code ec;
    std::string why;
    if (!ended_) why = "ended early (the download was cut off?)";
    else if (!opt_.required.empty() && !fs::exists(staging_ / opt_.required, ec))
        why = "no " + opt_.required + " folder: pack the game folder that holds " + opt_.required;
    if (!why.empty()) {
        cancel();
        failed_ = true;
        throw TarError(why);
    }
    fs::remove_all(dst_, ec);
    fs::rename(staging_, dst_, ec);
    done_ = true;
    if (ec) throw TarError(dst_.string() + ": " + ec.message());
}

std::string unpack_tar_file(const fs::path &tar, const fs::path &dst, const TarOptions &options,
                            const std::function<void(double)> &progress) {
    std::unique_ptr<std::FILE, int (*)(std::FILE *)> in(std::fopen(tar.string().c_str(), "rb"), std::fclose);
    if (!in) return tar.string() + ": " + std::strerror(errno);
    std::error_code ec;
    const uintmax_t total = fs::file_size(tar, ec);
    try {
        TarUnpacker u(dst, options);
        std::vector<uint8_t> buf(1 << 20);
        double shown = -1;
        size_t n;
        while ((n = std::fread(buf.data(), 1, buf.size(), in.get())) > 0) {
            u.feed(buf.data(), n);
            if (total && progress && double(u.bytes_fed()) / double(total) - shown >= 0.01) {
                shown = double(u.bytes_fed()) / double(total);
                progress(shown);
            }
        }
        u.finish();
    } catch (const TarError &e) {
        return tar.filename().string() + ": " + e.what();
    }
    return "";
}

} // namespace afc

// --- C interface (afcommon/tar_c.h) --------------------------------------------

struct afc_tar {
    std::unique_ptr<afc::TarUnpacker> u;
    std::string error;
};

extern "C" {

afc_tar *afc_tar_begin(const char *dst, const char *const *top_names, size_t top_count, const char *required) {
    auto *t = new afc_tar;
    afc::TarOptions o;
    for (size_t i = 0; i < top_count; ++i) o.top_names.push_back(top_names[i]);
    if (required) o.required = required;
    try {
        t->u = std::make_unique<afc::TarUnpacker>(dst, std::move(o));
    } catch (const std::exception &e) {
        t->error = e.what();
    }
    return t;
}

int afc_tar_feed(afc_tar *t, const void *data, size_t size) {
    if (!t->u) return -1;
    try {
        t->u->feed(static_cast<const uint8_t *>(data), size);
        return 0;
    } catch (const std::exception &e) {
        t->error = e.what();
        return -1;
    }
}

int afc_tar_finish(afc_tar *t) {
    if (!t->u) return -1;
    try {
        t->u->finish();
        return 0;
    } catch (const std::exception &e) {
        t->error = e.what();
        return -1;
    }
}

const char *afc_tar_error(const afc_tar *t) { return t->error.c_str(); }

void afc_tar_free(afc_tar *t) { delete t; }

} // extern "C"
