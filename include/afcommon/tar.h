// Tar unpacking for games kept as one file (a Lindbergh game as
// <set>.tar: a plain tar of the folder that holds disk0). Fed as the bytes
// arrive (a download) or from a file. ustar, GNU long names and links,
// base-256 sizes and pax headers. Anything that would land outside the
// destination ("..", absolute paths, a path through a symbolic link the tar
// made earlier) is refused, and macOS's own files (._*, __MACOSX,
// .DS_Store) are left out. The front ends share this one copy: the Linux
// front end through the C++ class, the Apple one through afcommon/tar_c.h.
#pragma once

#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <functional>
#include <stdexcept>
#include <string>
#include <vector>

namespace afc {

struct TarError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

// The most a long name ('L', 'K') or pax header ('x', 'g') may hold: they are
// kept in memory, and real ones are a few hundred bytes.
constexpr uint64_t kMaxTarHeaderBytes = uint64_t(1) << 20;

struct TarOptions {
    // A top folder with one of these names (any case) is dropped, so the tar
    // may hold either the game folder's contents or the folder itself.
    std::vector<std::string> top_names;
    // finish() refuses a result without this entry at the top ("disk0"); empty: no check.
    std::string required;
};

// Unpacks into <dst>.unpacking, then finish() puts it in place of dst.
class TarUnpacker {
public:
    TarUnpacker(std::filesystem::path dst, TarOptions options); // throws TarError
    ~TarUnpacker();                                             // cancels unless finished
    TarUnpacker(const TarUnpacker &) = delete;
    TarUnpacker &operator=(const TarUnpacker &) = delete;

    void feed(const uint8_t *data, size_t n); // throws TarError; nothing more is read after an error
    void finish();                            // throws TarError: cut off, or `required` missing
    void cancel();                            // removes what was unpacked so far
    uint64_t bytes_fed() const { return fed_; }

private:
    void start_entry();
    void end_entry();
    void create(const std::string &name, char type, const std::string &link, unsigned mode);
    std::filesystem::path inside(const std::string &name, bool &top, bool &metadata) const;
    void close_file();

    std::filesystem::path dst_, staging_;
    std::string base_; // staging_, resolved
    TarOptions opt_;
    uint8_t header_[512];
    size_t header_len_ = 0;
    std::FILE *file_ = nullptr;
    std::string collecting_;
    char collecting_type_ = 0;
    bool is_collecting_ = false;
    uint64_t remaining_ = 0, padding_ = 0, fed_ = 0, pax_size_ = UINT64_MAX;
    std::string long_name_, long_link_;
    bool ended_ = false, done_ = false, failed_ = false;
};

// Unpacks the tar file `tar` into dst. `progress` (0..1) is called as it goes.
// Returns "" or what went wrong.
std::string unpack_tar_file(const std::filesystem::path &tar, const std::filesystem::path &dst,
                            const TarOptions &options, const std::function<void(double)> &progress = {});

} // namespace afc
