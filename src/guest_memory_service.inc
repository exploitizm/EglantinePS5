// SPDX-License-Identifier: GPL-3.0-or-later
// ---- The guest's memory as a service: shared until written (included by memory_pages.cpp) ----
//
// After Gregory Price's "A Compressed RAM Service" (Linux Plumbers 2026, mm/cram.c): memory that
// is read where it lies and promoted when it is written. There the device compresses; here the
// emulator is the device, and the two forms a processor can read without a fault are a slot that
// shares the block of zeroes and a slot that shares a block with another slot of the same bytes.
//
//   Zero    the slot shows the shared block of zeroes, read-only
//   Own     the slot has a block of its own, read-write
//   Shared  the slot shows a block it shares with other slots of the same bytes, read-only
//   Fine    the slot is 128 pages of 16 KiB, read-only: its pages of zeroes show the zero block, the
//           others a compact block that holds only them (a written slot with many pages of zeroes);
//           a write makes it whole again, and it waits longer each time before it may be Fine again
//   Watch   an Own slot whose mapping is taken away while its block stays: any access faults, and the
//           slot shows its block again (the console)
//   Cold    a Watch slot no access touched for ColdAfterTicks: its bytes are kept LZ4-compressed in
//           the heap and its block goes back to the pool; an access faults, takes a block and has the
//           bytes back (the console). Guest memory a game loaded once and no longer reads (Aflakon,
//           viejunerd/Eglantine-PS5- issue 21: about 5 of 6 GB untouched for minutes in Ocarina)
//   Paged   the same for a slot that does not compress to half: its 2 MiB are written to a file
//           (PageFilePath), at the slot's own place in it, and read back when it is touched
//
// On the console a Zero slot is only reserved until it is touched: the first access faults and the
// slot shows the zero block from then on (a write then faults again, as below). The console maps at
// most about 32 GiB of direct memory for a process, and the zero block over every slot of a Switch 2
// game's 20 GiB took 19 GiB of it: graphics and the CPU's own mappings were refused at the rest
// (Mario Kart World, 2026-11-01). A slot that goes back is only reserved again.
//
// A write to a Zero or Shared slot faults; HandleGuestWriteFault gives the slot a block of its
// own (zeroed, or a copy) and the write repeats. So memory is no longer taken when the guest's
// kernel hands pages out, only when they are written; a slot the guest's kernel clears or frees
// whole goes back to the zero block; and a scanner gives back slots that hold only zeroes and
// lets slots of the same bytes share one block. A slot is locked (bit 7 of its state) while it
// changes; a writer that faults on a locked slot waits for it.
//
// 64-bit games only: a 32-bit game's fastmem window aliases the backing, which takes it whole.

namespace {
enum : std::uint8_t {
    SlotZero = 0, SlotOwn = 1, SlotShared = 2, SlotFine = 3, SlotWatch = 4, SlotCold = 5, SlotPaged = 6, SlotLock = 0x80,
    SlotMask = 0x7f
};
constexpr std::size_t FinePage = 16384, FinePages = SparseSlot / FinePage;
// A written slot becomes Fine with at least this many pages of zeroes (512 KiB given back).
constexpr std::size_t FineMinZeroPages = 32;
// A slot made whole again by a write waits 1, 2, 4 ... 1024 scan passes before it may be Fine again.
constexpr unsigned FineBackoffMax = 10;

// Duplicate sharing is off unless asked for (dev-settings cram_dedup=on): on the console it found a
// dozen 2 MiB pairs that were written apart within a minute, and with it on a large game held 20 FPS
// instead of 30 (the GPU worker waited for every fence, once a frame) where it held 30 without.
std::atomic<bool> service_requested{true}, scan_requested{true}, dedup_requested{false}, fine_requested{false};
std::atomic<SparseRange*> service_range{nullptr}; // the guest range the service runs for
std::atomic<SparseRange*> probe_range{nullptr};   // the self-test's range
std::atomic<int> service_state{0};                // 0 not tested, 1 works here, 2 does not
const char* service_refused = "";

struct Counters {
    std::atomic<std::uint64_t> faults{0}, promotions{0}, copies{0}, waits{0};
    std::atomic<std::uint64_t> returns_clear{0}, returns_free{0}, returns_scan{0}, dedup{0};
    std::atomic<std::uint64_t> reserve_used{0}, failures{0}, passes{0}, scan_ns{0};
    std::atomic<std::uint64_t> fined{0}, unfined{0}, fine_failures{0};
    std::atomic<std::uint64_t> shows{0}; // Zero slots that came to show the zero block (a first access)
    std::atomic<std::uint64_t> watched{0}, watch_hits{0}, colded{0}, cold_hits{0}, cold_kept{0}, cold_failures{0};
    std::atomic<std::uint64_t> cold_bytes{0}; // what the Cold slots' compressed bytes take now
    std::atomic<std::uint64_t> paged{0}, page_hits{0}, page_failures{0};
    // Time (diagnostic, claude/frame-rate-measure): the Watch/Cold ticks, their compression and page
    // writes, and the longest restore a fault waited for.
    std::atomic<std::uint64_t> tick_ns{0}, tick_max_ns{0}, compress_ns{0}, write_ns{0}, write_max_ns{0};
    std::atomic<std::uint64_t> restore_max_ns{0}, restore_ns{0}, restores{0}, read_ns{0}, read_max_ns{0};
    std::atomic<std::uint64_t> restore_pool_hits{0}, compresses{0}, compress_skips{0};
};
std::uint64_t NowNs() noexcept {
    timespec ts{};
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return std::uint64_t(ts.tv_sec) * 1000000000ull + std::uint64_t(ts.tv_nsec);
}
void NoteMax(std::atomic<std::uint64_t>& max, std::uint64_t value) noexcept {
    std::uint64_t seen = max.load(std::memory_order_relaxed);
    while (value > seen && !max.compare_exchange_weak(seen, value, std::memory_order_relaxed)) {
    }
}
Counters counters;
void ResetCounters() noexcept {
    for (auto* counter : {&counters.faults, &counters.promotions, &counters.copies, &counters.waits,
                          &counters.returns_clear, &counters.returns_free, &counters.returns_scan, &counters.dedup,
                          &counters.reserve_used, &counters.failures, &counters.passes, &counters.scan_ns,
                          &counters.fined, &counters.unfined, &counters.fine_failures, &counters.shows,
                          &counters.watched, &counters.watch_hits, &counters.colded, &counters.cold_hits,
                          &counters.cold_kept, &counters.cold_failures, &counters.cold_bytes, &counters.paged,
                          &counters.page_hits, &counters.page_failures})
        counter->store(0, std::memory_order_relaxed);
}

bool LockSlot(SparseRange& range, std::size_t slot, bool count_wait = false) noexcept {
    auto& state = range.state[slot];
    bool waited = false;
    for (;;) {
        std::uint8_t value = state.load(std::memory_order_relaxed);
        if (!(value & SlotLock) &&
            state.compare_exchange_weak(value, value | SlotLock, std::memory_order_acquire, std::memory_order_relaxed))
            break;
        waited = true;
        sched_yield();
    }
    if (waited && count_wait) counters.waits.fetch_add(1, std::memory_order_relaxed);
    return waited;
}
std::size_t FineKept(SparseRange& range, std::size_t slot) noexcept {
    return range.fine[slot] & 0xff;
}
std::uint8_t SlotState(SparseRange& range, std::size_t slot) noexcept {
    return range.state[slot].load(std::memory_order_acquire) & SlotMask;
}
// Sets the state and lets the slot go.
void UnlockSlot(SparseRange& range, std::size_t slot, std::uint8_t to) noexcept {
    range.owned[slot].store(to != SlotZero, std::memory_order_release);
    range.state[slot].store(to, std::memory_order_release);
}
std::uintptr_t SlotAddress(SparseRange& range, std::size_t slot) noexcept {
    return range.begin.load(std::memory_order_relaxed) + slot * SparseSlot;
}

// ---- Blocks: the pool, a reserve for when the pool says no, and shared blocks' references ----
//
// The reserve is the guest's headroom in the pool, held as blocks of its own: 1 GiB that a game's
// writes take when the pool has no block left. It used to be 32 MiB here, plus 1 GiB that the
// graphics caches only counted as taken (headless/performance.cpp): that 1 GiB stayed in the pool,
// where Eden's heap, the driver and the caches themselves could take it, and where 2 MiB blocks
// could no longer be cut from what was left. Tears of the Kingdom's writes found no block four
// times with the pool's largest free block at 1 or 2 MiB. Held here, nothing else can take it or
// cut it up; the caches see the pool as it is, less what is not held yet, and evict as soon as a
// burst of writes has used half of it (performance.h ViewOfPool). It is topped up every 100 ms
// (ReserveKeeper) and given back with the guest's memory when the session ends.
constexpr std::size_t ReserveBlocks = 512;      // 1 GiB
constexpr std::size_t ReserveAtStart = 16;      // taken as the service starts; the keeper takes the rest
std::int64_t reserve[ReserveBlocks];
std::atomic<std::size_t> reserve_count{0};
// Blocks the graphics caches borrowed when the pool had none left for an image or buffer
// (eden_guest_memory_lend): the keeper fills the reserve only to ReserveBlocks less these.
std::atomic<std::size_t> reserve_lent{0};
// At most three quarters of the reserve is lent: 256 MiB stays for the guest's writes. With all of it
// lent, a write of the guest (the GPU thread copying a blit into the guest's memory) found no block
// and ended the session at 155 s (Mario Kart World, 2026-11-01). With half lent, a race start ran out
// of graphics memory while the other 512 MiB stood unused (reserve_used=0, 2026-11-03).
constexpr std::size_t ReserveLendable = ReserveBlocks * 3 / 4;
std::atomic_flag reserve_lock = ATOMIC_FLAG_INIT;
constexpr std::size_t SharedBlocks = 4096;
std::int64_t shared_block[SharedBlocks];
int shared_refs[SharedBlocks];
std::atomic_flag shared_lock = ATOMIC_FLAG_INIT;

void Spin(std::atomic_flag& flag) noexcept {
    while (flag.test_and_set(std::memory_order_acquire)) sched_yield();
}

#ifdef PS5_NATIVE
// A zeroed block from the reserve (the pool refused one).
bool ReserveBlock(std::int64_t* physical) noexcept {
    Spin(reserve_lock);
    const std::size_t count = reserve_count.load(std::memory_order_relaxed);
    const bool ok = count > 0;
    if (ok) {
        *physical = reserve[count - 1];
        reserve_count.store(count - 1, std::memory_order_relaxed);
    }
    reserve_lock.clear(std::memory_order_release);
    if (ok) counters.reserve_used.fetch_add(1, std::memory_order_relaxed);
    return ok;
}
// Zeroed blocks into the reserve until it holds `blocks` (at most ReserveBlocks) or the pool says no.
void FillReserve(std::size_t blocks = ReserveBlocks) noexcept {
    blocks = std::min(blocks, ReserveBlocks);
    for (;;) {
        Spin(reserve_lock);
        const bool full = reserve_count.load(std::memory_order_relaxed) >= blocks;
        reserve_lock.clear(std::memory_order_release);
        if (full) return;
        std::int64_t physical = -1;
        if (!ZeroedBlock(&physical)) return;
        Spin(reserve_lock);
        const std::size_t count = reserve_count.load(std::memory_order_relaxed);
        if (count < blocks) {
            reserve[count] = physical;
            reserve_count.store(count + 1, std::memory_order_relaxed);
            physical = -1;
        }
        reserve_lock.clear(std::memory_order_release);
        if (physical >= 0 && sceKernelReleaseDirectMemory(physical, SparseSlot) != 0) std::abort();
    }
}
void EmptyReserve() noexcept {
    Spin(reserve_lock);
    for (std::size_t i = 0; i < reserve_count.load(std::memory_order_relaxed); ++i)
        if (sceKernelReleaseDirectMemory(reserve[i], SparseSlot) != 0) std::abort();
    reserve_count.store(0, std::memory_order_relaxed);
    reserve_lock.clear(std::memory_order_release);
}
#endif

// One more slot shows `physical`; the first one makes it a shared block. False when the table is full.
bool ShareBlock(std::int64_t physical) noexcept {
    Spin(shared_lock);
    std::size_t free_entry = SharedBlocks;
    bool done = false;
    for (std::size_t i = 0; i < SharedBlocks && !done; ++i) {
        if (shared_refs[i] > 0 && shared_block[i] == physical) {
            ++shared_refs[i];
            done = true;
        } else if (shared_refs[i] == 0 && free_entry == SharedBlocks) {
            free_entry = i;
        }
    }
    if (!done && free_entry < SharedBlocks) {
        shared_block[free_entry] = physical;
        shared_refs[free_entry] = 1;
        done = true;
    }
    shared_lock.clear(std::memory_order_release);
    return done;
}
// One slot less shows `physical`: the references left (0: the block is no longer shown).
int UnshareBlock(std::int64_t physical) noexcept {
    Spin(shared_lock);
    int left = 0;
    for (std::size_t i = 0; i < SharedBlocks; ++i)
        if (shared_refs[i] > 0 && shared_block[i] == physical) {
            left = --shared_refs[i];
            break;
        }
    shared_lock.clear(std::memory_order_release);
    return left;
}

// ---- The changes of a slot (the slot is locked) ----

#ifdef PS5_NATIVE
// A slot (or a run of them) only reserved again: the zero block no longer counts as mapped.
bool HideSlots(std::uintptr_t at, std::size_t bytes) noexcept {
    void* again = reinterpret_cast<void*>(at);
    return sceKernelReserveVirtualRange(&again, bytes, MAP_FIXED, SparseSlot) == 0 &&
           again == reinterpret_cast<void*>(at);
}
#endif
// A slot going back to Zero: only reserved where slots can be (the service hides them), else it
// shows the zero block.
void ShowNothing(SparseRange& range, std::size_t slot, std::uintptr_t at) noexcept {
#ifdef PS5_NATIVE
    if (range.shown != nullptr) {
        if (!HideSlots(at, SparseSlot)) std::abort();
        range.shown[slot].store(false, std::memory_order_release);
        return;
    }
    if (!MapSlot(at, PROT_READ, zero_block)) std::abort();
#else
    (void)range;
    (void)slot;
    (void)at;
#endif
}

// Zero -> Own: a zeroed block of its own, read-write.
bool PromoteZero(SparseRange& range, std::size_t slot) noexcept {
    const std::uintptr_t at = SlotAddress(range, slot);
#ifdef PS5_NATIVE
    std::int64_t physical = -1;
    if (!ZeroedBlock(&physical) && !ReserveBlock(&physical)) return false;
    if (!MapSlot(at, PROT_READ | PROT_WRITE, physical)) {
        (void)sceKernelReleaseDirectMemory(physical, SparseSlot);
        return false;
    }
    range.physical[slot] = physical;
#else
    if (mprotect(reinterpret_cast<void*>(at), SparseSlot, PROT_READ | PROT_WRITE) != 0) return false;
    range.physical[slot] = 0;
#endif
    guest_committed.fetch_add(SparseSlot, std::memory_order_relaxed);
    return true;
}

// Shared -> Own: the last slot to show a block takes it back read-write; the others copy it.
bool PromoteShared(SparseRange& range, std::size_t slot) noexcept {
    const std::uintptr_t at = SlotAddress(range, slot);
#ifdef PS5_NATIVE
    const std::int64_t shared = range.physical[slot];
    Spin(shared_lock);
    bool last = false;
    for (std::size_t i = 0; i < SharedBlocks; ++i)
        if (shared_refs[i] > 0 && shared_block[i] == shared) {
            last = shared_refs[i] == 1;
            if (last) shared_refs[i] = 0;
            break;
        }
    shared_lock.clear(std::memory_order_release);
    if (last) return mprotect(reinterpret_cast<void*>(at), SparseSlot, PROT_READ | PROT_WRITE) == 0;
    std::int64_t physical = -1;
    if (eden_direct_low(SparseSlot, SparseSlot, 12, &physical) != 0 && !ReserveBlock(&physical)) return false;
    void* view = reinterpret_cast<void*>(cpu_mapping_hint);
    if (sceKernelMapDirectMemory(&view, SparseSlot, PROT_READ | PROT_WRITE, 0, physical, SparseSlot) != 0) {
        (void)sceKernelReleaseDirectMemory(physical, SparseSlot);
        return false;
    }
    std::memcpy(view, reinterpret_cast<const void*>(at), SparseSlot);
    if (munmap(view, SparseSlot) != 0) std::abort();
    if (!MapSlot(at, PROT_READ | PROT_WRITE, physical)) {
        (void)sceKernelReleaseDirectMemory(physical, SparseSlot);
        return false;
    }
    range.physical[slot] = physical;
    if (UnshareBlock(shared) == 0 && sceKernelReleaseDirectMemory(shared, SparseSlot) != 0) std::abort();
    guest_committed.fetch_add(SparseSlot, std::memory_order_relaxed);
    counters.copies.fetch_add(1, std::memory_order_relaxed);
    return true;
#else
    (void)range;
    (void)slot;
    (void)at;
    return false; // the host never shares
#endif
}

void RetireCold(std::size_t slot) noexcept;
#ifdef PS5_NATIVE
void GivePagePlace(std::size_t slot) noexcept;
#endif

// Own, Shared, Fine, Watch or Cold -> Zero: the slot shows the zero block again and its memory goes back.
void ReturnSlot(SparseRange& range, std::size_t slot) noexcept {
    const std::uintptr_t at = SlotAddress(range, slot);
    const std::uint8_t state = SlotState(range, slot);
#ifdef PS5_NATIVE
    if (state == SlotCold || state == SlotPaged) {
        ShowNothing(range, slot, at);
        if (state == SlotCold) RetireCold(slot);
        else GivePagePlace(slot);
        range.physical[slot] = -1;
        return;
    }
    // A Watch slot is an Own slot not shown: the same below.
#endif
    if (state == SlotFine) {
        const std::size_t kept = FineKept(range, slot);
#ifdef PS5_NATIVE
        ShowNothing(range, slot, at);
        if (kept != 0 && sceKernelReleaseDirectMemory(range.physical[slot], kept * FinePage) != 0) std::abort();
#else
        if (madvise(reinterpret_cast<void*>(at), SparseSlot, MADV_DONTNEED) != 0) std::abort();
#endif
        guest_committed.fetch_sub(kept * FinePage, std::memory_order_relaxed);
        range.fine[slot] &= 0xf00;
        range.physical[slot] = -1;
        return;
    }
#ifdef PS5_NATIVE
    ShowNothing(range, slot, at);
    if (state == SlotShared) {
        if (UnshareBlock(range.physical[slot]) == 0) {
            if (sceKernelReleaseDirectMemory(range.physical[slot], SparseSlot) != 0) std::abort();
            guest_committed.fetch_sub(SparseSlot, std::memory_order_relaxed);
        }
    } else {
        if (sceKernelReleaseDirectMemory(range.physical[slot], SparseSlot) != 0) std::abort();
        guest_committed.fetch_sub(SparseSlot, std::memory_order_relaxed);
    }
#else
    (void)state;
    if (madvise(reinterpret_cast<void*>(at), SparseSlot, MADV_DONTNEED) != 0 ||
        mprotect(reinterpret_cast<void*>(at), SparseSlot, PROT_READ) != 0)
        std::abort();
    guest_committed.fetch_sub(SparseSlot, std::memory_order_relaxed);
#endif
    range.physical[slot] = -1;
}

bool AllZero(std::uintptr_t at) noexcept {
    const auto* word = reinterpret_cast<const volatile std::uint64_t*>(at);
    for (std::size_t i = 0; i < SparseSlot / 8; i += 8) {
        if (word[i] | word[i + 1] | word[i + 2] | word[i + 3] | word[i + 4] | word[i + 5] | word[i + 6] | word[i + 7])
            return false;
    }
    return true;
}

bool PageZero(std::uintptr_t at) noexcept {
    const auto* word = reinterpret_cast<const volatile std::uint64_t*>(at);
    for (std::size_t i = 0; i < FinePage / 8; i += 8)
        if (word[i] | word[i + 1] | word[i + 2] | word[i + 3] | word[i + 4] | word[i + 5] | word[i + 6] | word[i + 7])
            return false;
    return true;
}

// Fine -> Own: a written Fine slot becomes one block of 2 MiB again (a copy of what it shows), and
// stays whole for the rest of the session.
bool PromoteFine(SparseRange& range, std::size_t slot) noexcept {
    const std::uintptr_t at = SlotAddress(range, slot);
    const std::size_t kept = FineKept(range, slot);
#ifdef PS5_NATIVE
    std::int64_t physical = -1;
    if (eden_direct_low(SparseSlot, SparseSlot, 12, &physical) != 0 && !ReserveBlock(&physical)) return false;
    void* view = reinterpret_cast<void*>(cpu_mapping_hint);
    if (sceKernelMapDirectMemory(&view, SparseSlot, PROT_READ | PROT_WRITE, 0, physical, SparseSlot) != 0) {
        (void)sceKernelReleaseDirectMemory(physical, SparseSlot);
        return false;
    }
    std::memcpy(view, reinterpret_cast<const void*>(at), SparseSlot);
    if (munmap(view, SparseSlot) != 0) std::abort();
    if (!MapSlot(at, PROT_READ | PROT_WRITE, physical)) {
        (void)sceKernelReleaseDirectMemory(physical, SparseSlot);
        return false;
    }
    if (kept != 0 && sceKernelReleaseDirectMemory(range.physical[slot], kept * FinePage) != 0) std::abort();
    range.physical[slot] = physical;
#else
    if (mprotect(reinterpret_cast<void*>(at), SparseSlot, PROT_READ | PROT_WRITE) != 0) return false;
#endif
    guest_committed.fetch_add(SparseSlot - kept * FinePage, std::memory_order_relaxed);
    const unsigned times = std::min<unsigned>(((range.fine[slot] >> 8) & 0xf) + 1, FineBackoffMax);
    range.fine[slot] = static_cast<std::uint16_t>(times << 8);
    range.fine_after[slot] = static_cast<std::uint32_t>(counters.passes.load(std::memory_order_relaxed)) + (1u << times);
    counters.unfined.fetch_add(1, std::memory_order_relaxed);
    return true;
}

#ifdef PS5_NATIVE
bool MapRun(std::uintptr_t at, std::size_t bytes, std::int64_t physical) noexcept {
    void* address = reinterpret_cast<void*>(at);
    return sceKernelMapDirectMemory(&address, bytes, PROT_READ, MAP_FIXED, physical, FinePage) == 0 &&
           address == reinterpret_cast<void*>(at);
}
#endif

// Own -> Fine (the slot is locked and fenced, and has `zero` marked in a mask of its pages): its other
// pages go into a compact block, and the slot is mapped again page run by page run, read-only.
// The 2 MiB mapping is taken away whole first (a large mapping is never split); a thread that
// touches the slot meanwhile faults, waits for the lock and repeats its access.
bool MakeFine(SparseRange& range, std::size_t slot, const bool (&zero)[FinePages], std::size_t kept) noexcept {
    const std::uintptr_t at = SlotAddress(range, slot);
#ifdef PS5_NATIVE
    std::int64_t compact = -1;
    if (eden_direct_low(kept * FinePage, FinePage, 12, &compact) != 0) return false;
    void* view = reinterpret_cast<void*>(cpu_mapping_hint);
    if (sceKernelMapDirectMemory(&view, kept * FinePage, PROT_READ | PROT_WRITE, 0, compact, FinePage) != 0) {
        (void)sceKernelReleaseDirectMemory(compact, kept * FinePage);
        return false;
    }
    std::size_t next = 0;
    for (std::size_t page = 0; page < FinePages; ++page)
        if (!zero[page]) {
            std::memcpy(static_cast<std::uint8_t*>(view) + next * FinePage,
                        reinterpret_cast<const void*>(at + page * FinePage), FinePage);
            ++next;
        }
    if (munmap(view, kept * FinePage) != 0) std::abort();
    const std::int64_t whole = range.physical[slot];
    if (munmap(reinterpret_cast<void*>(at), SparseSlot) != 0) std::abort();
    bool ok = true;
    next = 0;
    for (std::size_t page = 0; page < FinePages && ok;) {
        std::size_t end = page + 1;
        while (end < FinePages && zero[end] == zero[page]) ++end;
        const std::size_t bytes = (end - page) * FinePage;
        ok = MapRun(at + page * FinePage, bytes, zero[page] ? zero_block : compact + std::int64_t(next * FinePage));
        if (!zero[page]) next += end - page;
        page = end;
    }
    if (!ok) {
        // The slot shows its block again, as it was (fenced: read-only until the caller unfences).
        if (!MapSlot(at, PROT_READ, whole)) std::abort();
        (void)sceKernelReleaseDirectMemory(compact, kept * FinePage);
        return false;
    }
    if (sceKernelReleaseDirectMemory(whole, SparseSlot) != 0) std::abort();
    range.physical[slot] = compact;
#else
    for (std::size_t page = 0; page < FinePages; ++page)
        if (zero[page] && madvise(reinterpret_cast<void*>(at + page * FinePage), FinePage, MADV_DONTNEED) != 0)
            std::abort();
#endif
    guest_committed.fetch_sub(SparseSlot - kept * FinePage, std::memory_order_relaxed);
    range.fine[slot] = static_cast<std::uint16_t>((range.fine[slot] & 0xf00) | kept);
    counters.fined.fetch_add(1, std::memory_order_relaxed);
    return true;
}

// ---- Watch and Cold (the console) ----
// Ticks of the scanner's Watch/Cold step: every 5 s (the scan of zeroes, Fine and duplicates every 4th).
constexpr unsigned ColdTickSeconds = 5;
// A slot no access touched for this many ticks in Watch goes Cold (10 s). At 30 s Ocarina's memory
// ran out 63 s in, before the first slot was due: 1.3 GiB untouched since about 35 s still waited.
constexpr std::uint32_t ColdAfterTicks = 2;
// At most this many Own slots go into Watch a tick (512 MiB), in turn over the range.
constexpr std::size_t WatchPerTick = 256;
// A slot that was touched in Watch, or did not compress, waits 1, 2, 4 ... 256 ticks before the next.
constexpr unsigned WatchBackoffMax = 8;
// Compressed to at most this (of 2 MiB), a slot stays in memory, Cold; else it is Paged: written to
// the page file whole (Ocarina, 19:13: 745 of 2,167 cooled slots did not compress to 3/4).
constexpr std::size_t ColdMaxBytes = SparseSlot / 2;
// Without the page file, a slot compressed to at most this still goes Cold.
constexpr std::size_t ColdMaxBytesNoFile = SparseSlot * 3 / 4;
// The page file: the guest's slots at their own offsets (slot x 2 MiB), only the written ones taking
// room. Made when the service starts, gone when its range is given back.
constexpr const char* PageFilePath = "/data/eglantineps5/cache/guest-pages-%02u.bin";
int page_fd = -1; // the first page file (>= 0: paging is on)
// The page file is PageFiles files, place p in file p % PageFiles at (p / PageFiles) x 2 MiB: one file's
// write holds it locked, and a restore reading another slot of the same file waited for it. On an idle
// console a 2 MiB write takes 14 ms and a read 0.5 to 1.4 ms (EDEN_PAGE_IO, 11-07 21:10); in gameplay,
// one file written all the time, both took about 140 ms.
constexpr std::size_t PageFiles = 16;
int page_fds[PageFiles] = {-1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1};
int PageFd(std::size_t place) noexcept {
    return page_fds[place % PageFiles];
}
off_t PageOffset(std::size_t place, std::size_t done) noexcept {
    return static_cast<off_t>((place / PageFiles) * SparseSlot + done);
}
// At most this many slots are written to the page files a tick (32 MiB in 5 s), so paging never fills
// the storage's queue while the game loads from it.
constexpr unsigned PagesPerTick = 16;
// While the pool is short: 64 slots a tick (128 MiB in 5 s, a write takes about 9 ms).
constexpr unsigned PagesPerTickShort = 64;
unsigned pages_this_tick = 0;
// At most this many slots are compressed a tick (about 5 ms each). Slots over it wait for the next
// tick; they only get older. At 21:23 the scanner spent 1.3 s a tick compressing, most of it on
// slots compressed again every tick because they did not compress to half and were not yet old
// enough to page: a slot in Watch is not touched (a touch ends Watch), so one result holds.
constexpr unsigned CompressPerTick = 96;
unsigned compresses_this_tick = 0;
// A slot that does not compress to half is Paged only while the pool is short (its largest free
// block under PageShortBytes) or once no access touched it for PageAfterTicks (30 s); until then it
// stays in Watch. Paged after 10 s, Ocarina read back 492 of 857 paged slots and its gameplay ran at
// 4 to 15 frames a second (19:21).
// 30 s (and at once while short) paged too eagerly: in gameplay the pool is short all the time, and
// half of what was paged was read back, each read a stall (20:41: 626 of 1,243). Then 2 minutes
// untouched, 1 minute while the pool is short. With 16 page files a read back takes about 1 ms, not
// 137 (21:23), and waiting a minute left the gameplay load with nothing paged: the guest grew from 4
// to 5 GiB in 10 s and the pool ran out (22:08, 22:19). Now 30 s untouched, 15 s while short.
constexpr std::uint32_t PageAfterTicks = 6;
constexpr std::uint32_t PageAfterTicksShort = 3;
constexpr std::size_t PageShortBytes = std::size_t{1} << 30;
// Places in the page file: a paged slot takes the lowest free one and gives it back when read back
// or given back, so the file stays compact and most writes go over room the file already has. At
// the slot's own offset in a sparse file, writes ran at 17 MB/s (20:41).
std::atomic<std::uint8_t>* page_place_used = nullptr;
// Blocks kept for restores: a block a slot gives back as it goes Cold or Paged comes here first
// (RestorePoolBlocks at most), and a restore takes one before asking the pool, so it does not
// wait for the graphics caches while the pool is short (20:41: restores of 47 ms on average, the
// longest 2.9 s).
constexpr std::size_t RestorePoolBlocks = 32;
std::int64_t restore_pool[RestorePoolBlocks];
std::atomic<std::size_t> restore_pool_count{0};
std::atomic_flag restore_pool_lock = ATOMIC_FLAG_INIT;
std::atomic<bool> pool_short{false};

struct ColdSlot {
    std::vector<std::uint8_t>* data = nullptr;                 // a Cold slot's compressed bytes
    std::atomic<std::vector<std::uint8_t>*> retired{nullptr};  // bytes a fault has used, freed by the scanner
    std::uint32_t since = 0;                                    // the tick it went into Watch
    std::uint32_t next = 0;                                     // the first tick it may go into Watch again
    std::uint8_t times = 0;                                     // backoffs so far
    std::int32_t place = -1;                                    // a Paged slot's place in the page file
    bool too_big = false;                                       // in this Watch, did not compress to half
};
ColdSlot* cold_slots = nullptr;
std::size_t cold_slot_count = 0;
std::atomic<std::uint32_t> cold_tick{0};

void ColdBackoff(std::size_t slot) noexcept {
    ColdSlot& cold = cold_slots[slot];
    cold.times = static_cast<std::uint8_t>(std::min<unsigned>(cold.times + 1u, WatchBackoffMax));
    cold.next = cold_tick.load(std::memory_order_relaxed) + (1u << cold.times);
}
#ifdef PS5_NATIVE
// The lowest free place in the page file (the scanner), -1 when none.
std::int32_t TakePagePlace() noexcept {
    if (page_place_used == nullptr) return -1;
    for (std::size_t place = 0; place < cold_slot_count; ++place) {
        std::uint8_t free_place = 0;
        if (page_place_used[place].compare_exchange_strong(free_place, 1, std::memory_order_acq_rel))
            return static_cast<std::int32_t>(place);
    }
    return -1;
}
void GivePagePlace(std::size_t slot) noexcept {
    ColdSlot& cold = cold_slots[slot];
    if (cold.place >= 0 && page_place_used != nullptr)
        page_place_used[cold.place].store(0, std::memory_order_release);
    cold.place = -1;
}
// A block for a restore, from the kept ones (true), else false.
bool TakeRestoreBlock(std::int64_t* physical) noexcept {
    Spin(restore_pool_lock);
    const std::size_t count = restore_pool_count.load(std::memory_order_relaxed);
    const bool ok = count > 0;
    if (ok) {
        *physical = restore_pool[count - 1];
        restore_pool_count.store(count - 1, std::memory_order_relaxed);
    }
    restore_pool_lock.clear(std::memory_order_release);
    if (ok) counters.restore_pool_hits.fetch_add(1, std::memory_order_relaxed);
    return ok;
}
// A block a slot gave back: kept for restores while there is room, else back to the pool.
void GiveBlock(std::int64_t physical) noexcept {
    Spin(restore_pool_lock);
    const std::size_t count = restore_pool_count.load(std::memory_order_relaxed);
    const bool kept = count < RestorePoolBlocks;
    if (kept) {
        restore_pool[count] = physical;
        restore_pool_count.store(count + 1, std::memory_order_relaxed);
    }
    restore_pool_lock.clear(std::memory_order_release);
    if (!kept && sceKernelReleaseDirectMemory(physical, SparseSlot) != 0) std::abort();
}
#endif

// A Cold slot given back (cleared or freed): its bytes go to the scanner to free.
void RetireCold(std::size_t slot) noexcept {
    ColdSlot& cold = cold_slots[slot];
    if (cold.data == nullptr) return;
    counters.cold_bytes.fetch_sub(cold.data->size(), std::memory_order_relaxed);
    cold.retired.store(cold.data, std::memory_order_release);
    cold.data = nullptr;
}

#ifdef PS5_NATIVE
// Watch -> Own: the slot shows its block again, read-write (the slot is locked).
bool RestoreWatch(SparseRange& range, std::size_t slot) noexcept {
    if (!MapSlot(SlotAddress(range, slot), PROT_READ | PROT_WRITE, range.physical[slot])) return false;
    ColdBackoff(slot);
    counters.watch_hits.fetch_add(1, std::memory_order_relaxed);
    return true;
}

// Cold -> Own: a block, its bytes back, read-write (the slot is locked). No allocation or free here:
// it runs in the fault handler; the compressed bytes go to `retired`, for the scanner.
bool RestoreCold(SparseRange& range, std::size_t slot) noexcept {
    ColdSlot& cold = cold_slots[slot];
    std::int64_t physical = -1;
    if (!TakeRestoreBlock(&physical) && eden_direct_low(SparseSlot, SparseSlot, 12, &physical) != 0 &&
        !ReserveBlock(&physical))
        return false;
    void* view = reinterpret_cast<void*>(cpu_mapping_hint);
    if (sceKernelMapDirectMemory(&view, SparseSlot, PROT_READ | PROT_WRITE, 0, physical, SparseSlot) != 0) {
        (void)sceKernelReleaseDirectMemory(physical, SparseSlot);
        return false;
    }
    const int written = guest_memory_decompress.load(std::memory_order_acquire)(view, SparseSlot, cold.data->data(),
                                                                                cold.data->size());
    if (written != static_cast<int>(SparseSlot)) std::abort(); // the bytes are the guest's: never lose them
    if (munmap(view, SparseSlot) != 0) std::abort();
    if (!MapSlot(SlotAddress(range, slot), PROT_READ | PROT_WRITE, physical)) {
        (void)sceKernelReleaseDirectMemory(physical, SparseSlot);
        return false;
    }
    range.physical[slot] = physical;
    guest_committed.fetch_add(SparseSlot, std::memory_order_relaxed);
    counters.cold_bytes.fetch_sub(cold.data->size(), std::memory_order_relaxed);
    cold.retired.store(cold.data, std::memory_order_release);
    cold.data = nullptr;
    ColdBackoff(slot);
    counters.cold_hits.fetch_add(1, std::memory_order_relaxed);
    return true;
}

// Paged -> Own: a block, its 2 MiB read back from the page file (pread: a system call, as the fault
// handler may make). A read that fails loses the guest's bytes: the app stops rather than go on
// with them wrong.
bool RestorePaged(SparseRange& range, std::size_t slot) noexcept {
    std::int64_t physical = -1;
    if (!TakeRestoreBlock(&physical) && eden_direct_low(SparseSlot, SparseSlot, 12, &physical) != 0 &&
        !ReserveBlock(&physical))
        return false;
    void* view = reinterpret_cast<void*>(cpu_mapping_hint);
    if (sceKernelMapDirectMemory(&view, SparseSlot, PROT_READ | PROT_WRITE, 0, physical, SparseSlot) != 0) {
        (void)sceKernelReleaseDirectMemory(physical, SparseSlot);
        return false;
    }
    const std::uint64_t read_began = NowNs();
    const std::size_t place = static_cast<std::size_t>(cold_slots[slot].place);
    std::size_t done = 0;
    while (done < SparseSlot) {
        const ssize_t got = pread(PageFd(place), static_cast<std::uint8_t*>(view) + done, SparseSlot - done,
                                  PageOffset(place, done));
        if (got <= 0) {
            if (got < 0 && errno == EINTR) continue;
            Note("EDEN_CRAM a paged slot of the guest's memory could not be read back from the page file\n");
            std::abort();
        }
        done += static_cast<std::size_t>(got);
    }
    const std::uint64_t read_took = NowNs() - read_began;
    counters.read_ns.fetch_add(read_took, std::memory_order_relaxed);
    NoteMax(counters.read_max_ns, read_took);
    if (munmap(view, SparseSlot) != 0) std::abort();
    if (!MapSlot(SlotAddress(range, slot), PROT_READ | PROT_WRITE, physical)) {
        (void)sceKernelReleaseDirectMemory(physical, SparseSlot);
        return false;
    }
    range.physical[slot] = physical;
    guest_committed.fetch_add(SparseSlot, std::memory_order_relaxed);
    GivePagePlace(slot);
    ColdBackoff(slot);
    counters.page_hits.fetch_add(1, std::memory_order_relaxed);
    return true;
}
#endif

// CommitSparsePage for a range with slot states.
bool ServiceCommit(SparseRange& range, std::size_t slot) noexcept {
    LockSlot(range, slot);
    const std::uint8_t state = SlotState(range, slot);
    bool ok = state == SlotZero ? PromoteZero(range, slot) : state == SlotFine ? PromoteFine(range, slot) : true;
#ifdef PS5_NATIVE
    if (state == SlotWatch) ok = RestoreWatch(range, slot);
    else if (state == SlotCold) ok = RestoreCold(range, slot);
    else if (state == SlotPaged) ok = RestorePaged(range, slot);
#endif
    const bool now_own =
        state == SlotZero || state == SlotFine || state == SlotWatch || state == SlotCold || state == SlotPaged;
    UnlockSlot(range, slot, ok && now_own ? SlotOwn : state);
    return ok;
}

// ---- The fault ----
SparseRange* WriteRangeOf(std::uintptr_t address) noexcept {
    for (SparseRange* range : {service_range.load(std::memory_order_acquire), probe_range.load(std::memory_order_acquire)}) {
        if (range == nullptr || range->state == nullptr) continue;
        const auto begin = range->begin.load(std::memory_order_acquire);
        if (begin != 0 && address >= begin && address < range->end) return range;
    }
    return nullptr;
}
std::atomic<std::uintptr_t> probe_expected{0}; // the self-test's write, whatever si_addr says
std::atomic<bool> probe_mismatch{false};
// The last write the service refused, for the crash report: the address, then why (1: it faulted
// again and again with nothing to change, 2: no block was left for its slot) and the slot's state.
std::atomic<std::uintptr_t> refused_address{0};
std::atomic<unsigned> refused_detail{0}; // why | state << 8
void Refused(std::uintptr_t address, unsigned why, std::uint8_t state) noexcept {
    refused_detail.store(why | unsigned{state} << 8, std::memory_order_relaxed);
    refused_address.store(address, std::memory_order_release);
}
} // namespace

// The last write the service refused (headless/crash_report.cpp): its address, 0 for none.
extern "C" std::uintptr_t eden_guest_write_refused(unsigned* why, unsigned* state) noexcept {
    const std::uintptr_t address = refused_address.load(std::memory_order_acquire);
    const unsigned detail = refused_detail.load(std::memory_order_relaxed);
    *why = detail & 0xff;
    *state = detail >> 8;
    return address;
}

#ifdef PS5_NATIVE
extern "C" const char __eden_text_start[] __attribute__((weak));
extern "C" const char __eden_text_end[] __attribute__((weak));
#endif

namespace {
// Who writes the slots that get promoted. A fault handler (ServiceSignal) reads the interrupted
// instruction's address and the first return address of the app's code above the stack pointer:
// a write by the emulated CPU has the JIT's entry point there, a write by the emulator itself the
// function that called the copy. The promotions are counted by that place (ServiceReport).
constexpr std::size_t WriterPcs = 4;
struct WriterSite {
    std::atomic<std::uintptr_t> key{0};
    std::uintptr_t pcs[WriterPcs]{};
    std::atomic<std::uint64_t> count{0};
};
constexpr std::size_t WriterSites = 512;
WriterSite writer_sites[WriterSites];
std::atomic<std::uint64_t> promotions_in_app_code{0}, promotions_elsewhere{0};
thread_local std::uintptr_t fault_pcs[WriterPcs] = {};
thread_local bool fault_in_app_code = false;

void NoteWriter() noexcept {
    (fault_in_app_code ? promotions_in_app_code : promotions_elsewhere).fetch_add(1, std::memory_order_relaxed);
    std::uintptr_t key = 0xCBF29CE484222325ull;
    for (const std::uintptr_t pc : fault_pcs) key = (key ^ pc) * 0x100000001B3ull;
    key |= 1;
    std::size_t at = static_cast<std::size_t>((key >> 12) % WriterSites);
    for (std::size_t probe = 0; probe < 16; ++probe, at = (at + 1) % WriterSites) {
        std::uintptr_t expected = 0;
        if (writer_sites[at].key.compare_exchange_strong(expected, key, std::memory_order_acq_rel)) {
            for (std::size_t i = 0; i < WriterPcs; ++i) writer_sites[at].pcs[i] = fault_pcs[i];
            writer_sites[at].count.fetch_add(1, std::memory_order_relaxed);
            return;
        }
        if (expected == key) {
            writer_sites[at].count.fetch_add(1, std::memory_order_relaxed);
            return;
        }
    }
}
} // namespace

namespace {
// Writers waiting for a block (WaitForBlock), and how many waits ended with one.
std::atomic<unsigned> memory_waiters{0};
std::atomic<std::uint64_t> memory_waits_won{0}, memory_waits_lost{0};

// No block was left for a written slot. Most of what the pool gives out is held by the graphics
// caches, which free what is not drawn once they see memory short (headless/performance.cpp reads
// eden_guest_memory_wanted, and while a writer waits the caches see no free memory at all). The
// game's own memory comes first: a large open-world game wrote 1.8 GB of new memory within 10 s
// after 23 minutes of play, faster than the caches free at the end of each frame, and the write
// that found no block ended the session. So the writer waits here, in its fault handler, and tries
// again every 20 ms for up to 2 s while the caches give memory back. Only then is it refused.
template <typename Promote>
bool WaitForBlock(Promote&& promote) noexcept {
    memory_waiters.fetch_add(1, std::memory_order_acq_rel);
    bool ok = false;
    for (unsigned tries = 0; tries < 100 && !ok; ++tries) {
        const timespec pause{0, 20'000'000};
        nanosleep(&pause, nullptr);
        ok = promote();
    }
    memory_waiters.fetch_sub(1, std::memory_order_acq_rel);
    (ok ? memory_waits_won : memory_waits_lost).fetch_add(1, std::memory_order_relaxed);
    Note(ok ? "EDEN_CRAM a written slot waited for the graphics caches to give memory back, and got a block\n"
            : "EDEN_CRAM a written slot waited 2 s for memory and found no block\n");
    return ok;
}
} // namespace

// How many writes of the guest's memory are waiting for a block now (headless/performance.cpp):
// while any is, the graphics caches are told no memory is free.
extern "C" unsigned eden_guest_memory_wanted(void) noexcept {
    return memory_waiters.load(std::memory_order_acquire);
}

// The guest's headroom (the reserve): what it holds and what it is to hold, in bytes; both 0 while
// the service does not run or keeps no reserve (the host). headless/performance.cpp counts what is
// not held yet as taken, as all of it was before.
extern "C" void eden_guest_memory_reserve(std::size_t* held, std::size_t* target) noexcept {
#ifdef PS5_NATIVE
    const bool running = service_range.load(std::memory_order_acquire) != nullptr;
    *held = running ? reserve_count.load(std::memory_order_relaxed) * SparseSlot : 0;
    *target = running ? ReserveBlocks * SparseSlot : 0;
#else
    *held = 0;
    *target = 0;
#endif
}

// The graphics caches found no room for an image or buffer even outside their budget, which ends
// the session: up to `bytes` of the reserve go back to the pool, and the keeper stops refilling
// them. The guest's writes then have less headroom; a session that ran out of memory for graphics
// went with all 1 GiB of the reserve unused. Returns the bytes given back (0: nothing to give).
extern "C" std::size_t eden_guest_memory_lend(std::size_t bytes) noexcept {
#ifdef PS5_NATIVE
    if (service_range.load(std::memory_order_acquire) == nullptr) return 0;
    std::size_t given = 0;
    while (given + SparseSlot <= bytes) {
        Spin(reserve_lock);
        const std::size_t count = reserve_count.load(std::memory_order_relaxed);
        std::int64_t physical = -1;
        if (count > 0 && reserve_lent.load(std::memory_order_relaxed) < ReserveLendable) {
            physical = reserve[count - 1];
            reserve_count.store(count - 1, std::memory_order_relaxed);
            reserve_lent.fetch_add(1, std::memory_order_relaxed);
        }
        reserve_lock.clear(std::memory_order_release);
        if (physical < 0) break;
        if (sceKernelReleaseDirectMemory(physical, SparseSlot) != 0) std::abort();
        given += SparseSlot;
    }
    return given;
#else
    (void)bytes;
    return 0;
#endif
}

// How the waits ended so far, and the writes refused for want of a block (headless/performance.cpp
// ReportMemory prints them beside what holds the pool).
extern "C" void eden_guest_memory_waits(unsigned long long* won, unsigned long long* lost,
                                        unsigned long long* refused) noexcept {
    *won = memory_waits_won.load(std::memory_order_relaxed);
    *lost = memory_waits_lost.load(std::memory_order_relaxed);
    *refused = counters.failures.load(std::memory_order_relaxed);
}

// A write fault at `address`: true when it was a write to a slot of the guest's memory that is
// now writable (the faulting instruction repeats), false for anything else.
bool HandleGuestWriteFault(void* fault) noexcept {
    auto address = reinterpret_cast<std::uintptr_t>(fault);
    if (const auto expected = probe_expected.load(std::memory_order_acquire); expected != 0 && address != expected) {
        probe_mismatch.store(true, std::memory_order_relaxed);
        address = expected;
    }
    SparseRange* const range = WriteRangeOf(address);
    if (range == nullptr) return false;
    const std::size_t slot = (address - range->begin.load(std::memory_order_relaxed)) / SparseSlot;
    // The same address faulting again and again with nothing to change is not a slot to promote
    // but a real fault. A promotion starts the count again (below).
    static thread_local std::uintptr_t last = 0;
    static thread_local unsigned repeats = 0;
    repeats = address == last ? repeats + 1 : 1;
    last = address;
    if (repeats > 16) {
        repeats = 0;
        Refused(address, 1, SlotState(*range, slot));
        Note("EDEN_CRAM refused a write: 17 faults in a row at one address, its slot unchanged\n");
        return false;
    }
    counters.faults.fetch_add(1, std::memory_order_relaxed);
    const bool waited = LockSlot(*range, slot, true);
    const std::uint8_t state = SlotState(*range, slot);
    if (waited) {
        // Someone changed the slot meanwhile (or is mapping it again): the access may now succeed,
        // and a read must never make a Fine slot whole. It faults again if it was a write.
        UnlockSlot(*range, slot, state);
        return true;
    }
#ifdef PS5_NATIVE
    // The first access to a Zero slot that is only reserved: it shows the zero block, and the access
    // repeats (a write faults again, and is promoted then).
    if (state == SlotZero && range->shown != nullptr && !range->shown[slot].load(std::memory_order_acquire)) {
        const bool shown = MapSlot(SlotAddress(*range, slot), PROT_READ, zero_block);
        if (shown) range->shown[slot].store(true, std::memory_order_release);
        UnlockSlot(*range, slot, state);
        if (!shown) {
            Refused(address, 3, state);
            Note("EDEN_CRAM a slot of the guest's memory could not show the zero block\n");
            return false;
        }
        counters.shows.fetch_add(1, std::memory_order_relaxed);
        repeats = 0;
        return true;
    }
#endif
#ifdef PS5_NATIVE
    // A slot being watched was touched: it shows its block again.
    if (state == SlotWatch) {
        const bool shown = RestoreWatch(*range, slot);
        UnlockSlot(*range, slot, shown ? SlotOwn : state);
        if (!shown) {
            Refused(address, 4, state);
            Note("EDEN_CRAM a watched slot of the guest's memory could not show its block again\n");
            return false;
        }
        repeats = 0;
        return true;
    }
#endif
    const auto promote = [&]() noexcept {
        bool done = true;
#ifdef PS5_NATIVE
        if (state == SlotCold || state == SlotPaged) {
            const std::uint64_t began = NowNs();
            const bool restored = state == SlotCold ? RestoreCold(*range, slot) : RestorePaged(*range, slot);
            const std::uint64_t took = NowNs() - began;
            counters.restore_ns.fetch_add(took, std::memory_order_relaxed);
            counters.restores.fetch_add(1, std::memory_order_relaxed);
            NoteMax(counters.restore_max_ns, took);
            return restored;
        }
#endif
        if (state == SlotFine) {
            done = PromoteFine(*range, slot);
        } else if (state == SlotZero) {
            done = PromoteZero(*range, slot);
            if (done) {
                counters.promotions.fetch_add(1, std::memory_order_relaxed);
                NoteWriter();
            }
        } else if (state == SlotShared) {
            done = PromoteShared(*range, slot);
        }
        return done;
    };
    bool ok = promote();
    if (!ok) ok = WaitForBlock(promote);
    UnlockSlot(*range, slot, ok ? SlotOwn : state);
    if (!ok) {
        counters.failures.fetch_add(1, std::memory_order_relaxed);
        Refused(address, 2, state);
        Note("EDEN_CRAM out of memory: a written slot of the guest's memory found no block\n");
    } else if (state != SlotOwn) {
        // The slot was made writable. A game that writes one place each time its slot was given
        // back (cleared, freed or scanned) faults there again and again over a session, each time
        // with something to do: counted as repeats, the 17th such write ended the game after
        // 15 to 60 minutes (Swordpdf/ProsperoEden#246).
        repeats = 0;
    }
    return ok;
}

namespace {
struct sigaction service_previous_segv{}, service_previous_bus{};

void ServiceSignal(int signal, siginfo_t* info, void* context) {
#ifdef PS5_NATIVE
    fault_in_app_code = false;
    for (std::uintptr_t& pc : fault_pcs) pc = 0;
    if (context != nullptr) {
        const auto low = reinterpret_cast<std::uintptr_t>(__eden_text_start) + 8;
        const auto high = reinterpret_cast<std::uintptr_t>(__eden_text_end);
        const std::uintptr_t rip = Eden::Fastmem::ContextRip(context);
        fault_in_app_code = rip >= low && rip < high;
        const auto* stack = reinterpret_cast<const std::uintptr_t*>(Eden::Fastmem::ContextRsp(context));
        std::size_t found = 0;
        // Never past the page (16 KiB) that holds the stack pointer: a thread's stack can end a few
        // hundred bytes above it, and what lies after it is not mapped.
        const auto page_end = ((reinterpret_cast<std::uintptr_t>(stack) | 0x3FFFu) + 1);
        const auto room = static_cast<unsigned>((page_end - reinterpret_cast<std::uintptr_t>(stack)) / sizeof(std::uintptr_t));
        for (unsigned i = 0; i < 160 && i < room && found < WriterPcs; ++i) {
            if (stack[i] >= low && stack[i] < high && (found == 0 || fault_pcs[found - 1] != stack[i])) {
                fault_pcs[found++] = stack[i];
            }
        }
    }
#endif
    if (HandleGuestWriteFault(info ? info->si_addr : nullptr)) return;
    const struct sigaction& previous = signal == SIGSEGV ? service_previous_segv : service_previous_bus;
    if (previous.sa_flags & SA_SIGINFO) {
        previous.sa_sigaction(signal, info, context);
    } else if (previous.sa_handler == SIG_DFL) {
        std::signal(signal, SIG_DFL); // the faulting instruction repeats and ends the process
    } else if (previous.sa_handler != SIG_IGN) {
        previous.sa_handler(signal);
    }
}

bool InstallServiceSignal() noexcept {
    static std::once_flag once;
    static bool installed = false;
    std::call_once(once, [] {
        struct sigaction action{};
        action.sa_sigaction = ServiceSignal;
        action.sa_flags = SA_SIGINFO | SA_RESTART;
        sigemptyset(&action.sa_mask);
        installed = sigaction(SIGSEGV, &action, &service_previous_segv) == 0 &&
                    sigaction(SIGBUS, &action, &service_previous_bus) == 0;
    });
    return installed;
}

// Gives a range slot states (a guest range, or the self-test's).
bool AddStates(SparseRange& range) noexcept {
    const std::size_t slots = (range.end - range.begin.load(std::memory_order_relaxed)) / SparseSlot;
    auto* state = new (std::nothrow) std::atomic<std::uint8_t>[slots];
    auto* fine = state ? new (std::nothrow) std::uint16_t[slots]() : nullptr;
    auto* fine_after = fine ? new (std::nothrow) std::uint32_t[slots]() : nullptr;
    if (fine_after == nullptr) {
        delete[] state;
        delete[] fine;
        return false;
    }
    range.fine = fine;
    range.fine_after = fine_after;
    for (std::size_t slot = 0; slot < slots; ++slot)
        state[slot].store(range.owned[slot].load(std::memory_order_relaxed) ? SlotOwn : SlotZero, std::memory_order_relaxed);
    range.state = state;
    return true;
}

#ifdef PS5_NATIVE
// As the service starts, with its fault handler in place: every Zero slot only reserved, in runs (one
// call each). A slot is marked as not showing the zero block before it stops showing it, so an access
// meanwhile either reads zeroes or faults and is shown it again.
void HideZeroSlots(SparseRange& range) noexcept {
    const std::uintptr_t begin = range.begin.load(std::memory_order_relaxed);
    const std::size_t slots = (range.end - begin) / SparseSlot;
    auto* shown = new (std::nothrow) std::atomic<bool>[slots];
    if (shown == nullptr) return; // they keep showing the zero block
    for (std::size_t slot = 0; slot < slots; ++slot) shown[slot].store(true, std::memory_order_relaxed);
    range.shown = shown;
    std::atomic_thread_fence(std::memory_order_release);
    std::size_t hidden = 0;
    for (std::size_t slot = 0; slot < slots;) {
        if (SlotState(range, slot) != SlotZero) {
            ++slot;
            continue;
        }
        // The slots of the run locked (a slot promoted meanwhile ends the run), so none changes under it.
        std::size_t end = slot;
        while (end < slots && end - slot < 512) {
            LockSlot(range, end);
            if (SlotState(range, end) != SlotZero) {
                UnlockSlot(range, end, SlotState(range, end));
                break;
            }
            ++end;
        }
        if (end == slot) {
            ++slot;
            continue;
        }
        for (std::size_t i = slot; i < end; ++i) shown[i].store(false, std::memory_order_release);
        const bool ok = HideSlots(begin + slot * SparseSlot, (end - slot) * SparseSlot);
        if (!ok) {
            for (std::size_t i = slot; i < end; ++i) shown[i].store(true, std::memory_order_release);
            // A run the kernel would not reserve again: its slots show the zero block, one by one.
            for (std::size_t i = slot; i < end; ++i)
                if (!MapSlot(begin + i * SparseSlot, PROT_READ, zero_block)) std::abort();
        } else {
            hidden += end - slot;
        }
        for (std::size_t i = slot; i < end; ++i) UnlockSlot(range, i, SlotZero);
        slot = end;
    }
    char line[96];
    const int length = std::snprintf(line, sizeof(line), "EDEN_CRAM hidden zero slots: %zu of %zu (%zu MiB)\n",
                                     hidden, slots, hidden * (SparseSlot >> 20));
    if (length > 0) Report(line);
}
#endif

// ---- The scanner ----
#ifdef PS5_NATIVE
// Own -> Watch: the slot's mapping goes (its block stays); the next access faults (RestoreWatch).
bool WatchSlot(SparseRange& range, std::size_t slot, std::uint32_t tick) noexcept {
    LockSlot(range, slot);
    if (SlotState(range, slot) != SlotOwn || tick < cold_slots[slot].next) {
        UnlockSlot(range, slot, SlotState(range, slot));
        return false;
    }
    if (!HideSlots(SlotAddress(range, slot), SparseSlot)) {
        UnlockSlot(range, slot, SlotOwn);
        return false;
    }
    cold_slots[slot].since = tick;
    cold_slots[slot].too_big = false;
    UnlockSlot(range, slot, SlotWatch);
    counters.watched.fetch_add(1, std::memory_order_relaxed);
    return true;
}

// Watch -> Cold: a slot no access touched since it went into Watch is compressed and its block goes
// back. One that does not compress shows its block again and waits.
bool CoolSlot(SparseRange& range, std::size_t slot, std::uint32_t idle) noexcept {
    LockSlot(range, slot);
    if (SlotState(range, slot) != SlotWatch) {
        UnlockSlot(range, slot, SlotState(range, slot));
        return false;
    }
    const bool short_now = pool_short.load(std::memory_order_relaxed);
    const bool may_page = page_fd >= 0 && pages_this_tick < (short_now ? PagesPerTickShort : PagesPerTick) &&
                          (idle >= PageAfterTicks || (idle >= PageAfterTicksShort && short_now));
    const bool too_big = cold_slots[slot].too_big;
    if ((too_big && !may_page) || (!too_big && compresses_this_tick >= CompressPerTick)) {
        // Known not to compress to half and not old enough to page, or out of this tick's
        // compressions: it stays in Watch untouched.
        if (too_big) counters.compress_skips.fetch_add(1, std::memory_order_relaxed);
        UnlockSlot(range, slot, SlotWatch);
        return false;
    }
    const std::int64_t physical = range.physical[slot];
    void* view = reinterpret_cast<void*>(cpu_mapping_hint);
    bool cooled = false;
    if (sceKernelMapDirectMemory(&view, SparseSlot, PROT_READ, 0, physical, SparseSlot) == 0) {
        // The scanner's own room for one slot compressed (LZ4's bound for 2 MiB).
        static std::vector<std::uint8_t> room(SparseSlot + SparseSlot / 255 + 16);
        std::size_t packed = 0;
        if (too_big) {
            counters.compress_skips.fetch_add(1, std::memory_order_relaxed);
        } else {
            ++compresses_this_tick;
            counters.compresses.fetch_add(1, std::memory_order_relaxed);
            const std::uint64_t compress_began = NowNs();
            packed = guest_memory_compress.load(std::memory_order_acquire)(view, SparseSlot, room.data(), room.size());
            counters.compress_ns.fetch_add(NowNs() - compress_began, std::memory_order_relaxed);
        }
        // Not to half: the page file takes it whole, where there is one.
        bool written = false;
        if ((packed == 0 || packed > ColdMaxBytes) && page_fd >= 0 && !may_page) {
            // Not to half, and not to be paged yet: it stays in Watch, its block kept, and is not
            // compressed again while it stays there.
            cold_slots[slot].too_big = true;
            if (munmap(view, SparseSlot) != 0) std::abort();
            UnlockSlot(range, slot, SlotWatch);
            return false;
        }
        const std::int32_t place = (packed == 0 || packed > ColdMaxBytes) && may_page ? TakePagePlace() : -1;
        if (place >= 0) {
            const std::uint64_t write_began = NowNs();
            std::size_t done = 0;
            while (done < SparseSlot) {
                const ssize_t put = pwrite(PageFd(std::size_t(place)), static_cast<const std::uint8_t*>(view) + done,
                                           SparseSlot - done, PageOffset(std::size_t(place), done));
                if (put <= 0) {
                    if (put < 0 && errno == EINTR) continue;
                    break;
                }
                done += static_cast<std::size_t>(put);
            }
            written = done == SparseSlot;
            ++pages_this_tick;
            if (written) cold_slots[slot].place = place;
            else {
                counters.page_failures.fetch_add(1, std::memory_order_relaxed);
                page_place_used[place].store(0, std::memory_order_release);
            }
            const std::uint64_t write_took = NowNs() - write_began;
            counters.write_ns.fetch_add(write_took, std::memory_order_relaxed);
            NoteMax(counters.write_max_ns, write_took);
        }
        if (munmap(view, SparseSlot) != 0) std::abort();
        if (written) {
            GiveBlock(physical);
            range.physical[slot] = -1;
            guest_committed.fetch_sub(SparseSlot, std::memory_order_relaxed);
            counters.paged.fetch_add(1, std::memory_order_relaxed);
            UnlockSlot(range, slot, SlotPaged);
            return true;
        }
        if (packed != 0 && packed <= (page_fd >= 0 ? ColdMaxBytes : ColdMaxBytesNoFile)) {
            cold_slots[slot].data =
                new (std::nothrow) std::vector<std::uint8_t>(room.begin(), room.begin() + static_cast<std::ptrdiff_t>(packed));
            if (cold_slots[slot].data != nullptr) {
                GiveBlock(physical);
                range.physical[slot] = -1;
                guest_committed.fetch_sub(SparseSlot, std::memory_order_relaxed);
                counters.cold_bytes.fetch_add(cold_slots[slot].data->size(), std::memory_order_relaxed);
                counters.colded.fetch_add(1, std::memory_order_relaxed);
                cooled = true;
            }
        } else {
            counters.cold_kept.fetch_add(1, std::memory_order_relaxed);
        }
    } else {
        counters.cold_failures.fetch_add(1, std::memory_order_relaxed);
    }
    if (!cooled) {
        // Shown again; a slot that does not compress waits the longest before it is looked at again.
        if (!MapSlot(SlotAddress(range, slot), PROT_READ | PROT_WRITE, physical)) std::abort();
        cold_slots[slot].times = WatchBackoffMax;
        cold_slots[slot].next = cold_tick.load(std::memory_order_relaxed) + (1u << WatchBackoffMax);
    }
    UnlockSlot(range, slot, cooled ? SlotCold : SlotOwn);
    return cooled;
}

// One Watch/Cold tick: the bytes faults used are freed, watched slots untouched long enough go
// Cold, and the next Own slots in turn go into Watch.
void ColdTickBody(SparseRange& range) noexcept;
void ColdTick(SparseRange& range) noexcept {
    if (cold_slots == nullptr || guest_memory_compress.load(std::memory_order_acquire) == nullptr) return;
    const std::uint64_t began = NowNs();
    ColdTickBody(range);
    const std::uint64_t took = NowNs() - began;
    counters.tick_ns.fetch_add(took, std::memory_order_relaxed);
    NoteMax(counters.tick_max_ns, took);
}
void ColdTickBody(SparseRange& range) noexcept {
    pages_this_tick = 0;
    compresses_this_tick = 0;
    const std::uint32_t tick = cold_tick.fetch_add(1, std::memory_order_relaxed) + 1;
    {
        const std::int64_t total = sceKernelGetDirectMemorySize();
        std::int64_t start = 0;
        std::size_t largest = 0;
        pool_short.store(total > 0 && sceKernelAvailableDirectMemorySize(0, total, 0x4000, &start, &largest) == 0 &&
                             largest < PageShortBytes,
                         std::memory_order_relaxed);
    }
    static std::size_t cursor = 0;
    for (std::size_t slot = 0; slot < cold_slot_count; ++slot) {
        if (auto* retired = cold_slots[slot].retired.exchange(nullptr, std::memory_order_acq_rel)) delete retired;
        if (SlotState(range, slot) == SlotWatch && tick - cold_slots[slot].since >= ColdAfterTicks)
            (void)CoolSlot(range, slot, tick - cold_slots[slot].since);
    }
    std::size_t watched = 0;
    for (std::size_t seen = 0; seen < cold_slot_count && watched < WatchPerTick; ++seen) {
        cursor = (cursor + 1) % cold_slot_count;
        if (SlotState(range, cursor) == SlotOwn && tick >= cold_slots[cursor].next) watched += WatchSlot(range, cursor, tick);
    }
}
#endif

std::thread scanner;
std::mutex scanner_mutex;
std::condition_variable scanner_wake;
bool scanner_stop = false;
#ifdef PS5_NATIVE
// The reserve kept full: every 100 ms, so a burst of writes that used it finds it whole again as
// soon as the caches have given memory back (the scanner only passes every 20 s).
std::thread keeper;

void ReserveKeeper() {
    std::unique_lock lock{scanner_mutex};
    while (!scanner_stop) {
        scanner_wake.wait_for(lock, std::chrono::milliseconds(100), [] { return scanner_stop; });
        if (scanner_stop) break;
        lock.unlock();
        FillReserve(ReserveBlocks - std::min(reserve_lent.load(std::memory_order_relaxed), ReserveBlocks));
        lock.lock();
    }
}
#endif

std::uint64_t Mix(std::uint64_t hash, std::uint64_t word) noexcept {
    hash ^= word + 0x9e3779b97f4a7c15ull + (hash << 6) + (hash >> 2);
    return hash * 0xff51afd7ed558ccdull;
}
std::uint64_t SampleHash(std::uintptr_t at) noexcept {
    const auto* word = reinterpret_cast<const volatile std::uint64_t*>(at);
    std::uint64_t hash = 0;
    for (std::size_t i = 0; i < SparseSlot / 8; i += SparseSlot / 8 / 64) hash = Mix(hash, word[i] ^ word[i + 1]);
    return hash;
}
std::uint64_t FullHash(std::uintptr_t at, std::size_t bytes) noexcept {
    const auto* word = reinterpret_cast<const volatile std::uint64_t*>(at);
    std::uint64_t hash = 0;
    for (std::size_t i = 0; i < bytes / 8; ++i) hash = Mix(hash, word[i]);
    return hash;
}

void Fence(std::uintptr_t at) noexcept {
    if (mprotect(reinterpret_cast<void*>(at), SparseSlot, PROT_READ) != 0) std::abort();
}
void Unfence(std::uintptr_t at) noexcept {
    if (mprotect(reinterpret_cast<void*>(at), SparseSlot, PROT_READ | PROT_WRITE) != 0) std::abort();
}

// An Own slot that holds only zeroes goes back. Fenced while it is looked at again.
bool ScanZero(SparseRange& range, std::size_t slot) noexcept {
    LockSlot(range, slot);
    if (SlotState(range, slot) != SlotOwn) {
        UnlockSlot(range, slot, SlotState(range, slot));
        return false;
    }
    const std::uintptr_t at = SlotAddress(range, slot);
    Fence(at);
    const bool zero = AllZero(at);
    if (zero) ReturnSlot(range, slot);
    else Unfence(at);
    UnlockSlot(range, slot, zero ? SlotZero : SlotOwn);
    return zero;
}

// An Own slot with many pages of zeroes becomes Fine. A racy count first, then the count again fenced.
bool ScanFine(SparseRange& range, std::size_t slot) noexcept {
    const std::uintptr_t at = SlotAddress(range, slot);
    if (counters.passes.load(std::memory_order_relaxed) < range.fine_after[slot]) return false;
    std::size_t zeros = 0;
    for (std::size_t page = 0; page < FinePages; ++page) zeros += PageZero(at + page * FinePage);
    if (zeros < FineMinZeroPages) return false;
    LockSlot(range, slot);
    if (SlotState(range, slot) != SlotOwn) {
        UnlockSlot(range, slot, SlotState(range, slot));
        return false;
    }
    Fence(at);
    bool zero[FinePages];
    zeros = 0;
    for (std::size_t page = 0; page < FinePages; ++page) zeros += zero[page] = PageZero(at + page * FinePage);
    bool fined = false;
    if (zeros == FinePages) {
        ReturnSlot(range, slot);
        UnlockSlot(range, slot, SlotZero);
        counters.returns_scan.fetch_add(1, std::memory_order_relaxed);
        return true;
    }
    if (zeros >= FineMinZeroPages) {
        fined = MakeFine(range, slot, zero, FinePages - zeros);
        if (!fined) counters.fine_failures.fetch_add(1, std::memory_order_relaxed);
    }
    if (!fined) Unfence(at);
    UnlockSlot(range, slot, fined ? SlotFine : SlotOwn);
    return fined;
}

// Two slots of the same bytes show one block (the console; the host only counts them).
bool ScanShare(SparseRange& range, std::size_t keep, std::size_t other) noexcept {
    const std::size_t first = std::min(keep, other), second = std::max(keep, other);
    LockSlot(range, first);
    LockSlot(range, second);
    const std::uint8_t keep_state = SlotState(range, keep), other_state = SlotState(range, other);
    bool shared = false;
    if ((keep_state == SlotOwn || keep_state == SlotShared) && other_state == SlotOwn) {
        const std::uintptr_t keep_at = SlotAddress(range, keep), other_at = SlotAddress(range, other);
        if (keep_state == SlotOwn) Fence(keep_at);
        Fence(other_at);
        const bool same = std::memcmp(reinterpret_cast<const void*>(keep_at), reinterpret_cast<const void*>(other_at),
                                      SparseSlot) == 0;
#ifdef PS5_NATIVE
        // A block becoming shared counts both slots (the first ShareBlock makes it 1, the second 2); one
        // already shared counts one more. A table that is full shares nothing.
        bool counted = false;
        if (same && ShareBlock(range.physical[keep])) {
            counted = keep_state == SlotShared || ShareBlock(range.physical[keep]);
            if (!counted) (void)UnshareBlock(range.physical[keep]);
        }
        if (counted) {
            if (!MapSlot(other_at, PROT_READ, range.physical[keep])) std::abort();
            if (sceKernelReleaseDirectMemory(range.physical[other], SparseSlot) != 0) std::abort();
            range.physical[other] = range.physical[keep];
            guest_committed.fetch_sub(SparseSlot, std::memory_order_relaxed);
            shared = true;
        }
#else
        (void)same;
#endif
        if (!shared) {
            if (keep_state == SlotOwn) Unfence(keep_at);
            Unfence(other_at);
        }
        if (same) counters.dedup.fetch_add(1, std::memory_order_relaxed);
    }
    UnlockSlot(range, second, second == keep ? (shared ? SlotShared : keep_state) : (shared ? SlotShared : other_state));
    UnlockSlot(range, first, first == keep ? (shared ? SlotShared : keep_state) : (shared ? SlotShared : other_state));
    return shared;
}

void ScanPass(SparseRange& range) noexcept {
    const auto start = std::chrono::steady_clock::now();
    const std::size_t slots = (range.end - range.begin.load(std::memory_order_relaxed)) / SparseSlot;
    std::vector<std::pair<std::uint64_t, std::size_t>> samples;
    for (std::size_t slot = 0; slot < slots; ++slot) {
        if (SlotState(range, slot) != SlotOwn) continue;
        const std::uintptr_t at = SlotAddress(range, slot);
        if (AllZero(at)) { // a read without the lock: only a hint, checked again fenced
            if (ScanZero(range, slot)) counters.returns_scan.fetch_add(1, std::memory_order_relaxed);
        } else if (fine_requested.load(std::memory_order_relaxed) && ScanFine(range, slot)) {
            // now Fine
        } else if (dedup_requested.load(std::memory_order_relaxed)) {
            samples.emplace_back(SampleHash(at), slot);
        }
    }
    std::sort(samples.begin(), samples.end());
    for (std::size_t i = 0; i < samples.size();) {
        std::size_t j = i + 1;
        while (j < samples.size() && samples[j].first == samples[i].first) ++j;
        if (j - i > 1) {
            std::vector<std::pair<std::uint64_t, std::size_t>> full;
            for (std::size_t k = i; k < j; ++k)
                full.emplace_back(FullHash(SlotAddress(range, samples[k].second), SparseSlot), samples[k].second);
            std::sort(full.begin(), full.end());
            for (std::size_t a = 0; a < full.size();) {
                std::size_t b = a + 1;
                while (b < full.size() && full[b].first == full[a].first) {
                    (void)ScanShare(range, full[a].second, full[b].second);
                    ++b;
                }
                a = b;
            }
        }
        i = j;
    }
    counters.passes.fetch_add(1, std::memory_order_relaxed);
    counters.scan_ns.fetch_add(static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now() - start).count()), std::memory_order_relaxed);
}

void ServiceReport(SparseRange& range, const char* when) noexcept {
    const std::size_t slots = (range.end - range.begin.load(std::memory_order_relaxed)) / SparseSlot;
    std::size_t own = 0, shared = 0, fine = 0, fine_kept = 0, zero_shown = 0, watch = 0, cold = 0, paged = 0;
    for (std::size_t slot = 0; slot < slots; ++slot) {
        const auto state = SlotState(range, slot);
        zero_shown += state == SlotZero && (range.shown == nullptr || range.shown[slot].load(std::memory_order_relaxed));
        own += state == SlotOwn;
        shared += state == SlotShared;
        watch += state == SlotWatch;
        cold += state == SlotCold;
        paged += state == SlotPaged;
        if (state == SlotFine) {
            ++fine;
            fine_kept += FineKept(range, slot);
        }
    }
    char line[640];
    const int length = std::snprintf(line, sizeof(line),
        "EDEN_CRAM %s held=%zuMiB own=%zu shared=%zu zero=%zu (showing the zero block %zu, first accesses %llu) faults=%llu promotions=%llu copies=%llu waits=%llu "
        "returns_clear=%llu returns_free=%llu returns_scan=%llu dedup=%llu reserve_used=%llu failures=%llu "
        "passes=%llu scan_ms=%llu fine=%zu fine_held=%zuMiB fined=%llu unfined=%llu fine_failures=%llu "
        "reserve=%zuMiB\n",
        when, guest_committed.load(std::memory_order_relaxed) >> 20, own, shared,
        slots - own - shared - fine - watch - cold - paged,
        zero_shown, (unsigned long long)counters.shows.load(),
        (unsigned long long)counters.faults.load(), (unsigned long long)counters.promotions.load(),
        (unsigned long long)counters.copies.load(), (unsigned long long)counters.waits.load(),
        (unsigned long long)counters.returns_clear.load(), (unsigned long long)counters.returns_free.load(),
        (unsigned long long)counters.returns_scan.load(), (unsigned long long)counters.dedup.load(),
        (unsigned long long)counters.reserve_used.load(), (unsigned long long)counters.failures.load(),
        (unsigned long long)counters.passes.load(), (unsigned long long)(counters.scan_ns.load() / 1000000),
        fine, fine_kept * FinePage >> 20, (unsigned long long)counters.fined.load(),
        (unsigned long long)counters.unfined.load(), (unsigned long long)counters.fine_failures.load(),
        reserve_count.load(std::memory_order_relaxed) * (SparseSlot >> 20));
    if (length > 0) Report(line);
    {
        char cold_line[512];
        const int cold_length = std::snprintf(cold_line, sizeof(cold_line),
            "EDEN_CRAM_COLD %s watch=%zu cold=%zu (%zu MiB of slots in %llu MiB) paged=%zu (%zu MiB) watched=%llu "
            "touched=%llu cooled=%llu restored=%llu paged_out=%llu paged_in=%llu page_failures=%llu "
            "incompressible=%llu failures=%llu\n",
            when, watch, cold, cold * (SparseSlot >> 20), (unsigned long long)(counters.cold_bytes.load() >> 20), paged,
            paged * (SparseSlot >> 20), (unsigned long long)counters.watched.load(),
            (unsigned long long)counters.watch_hits.load(), (unsigned long long)counters.colded.load(),
            (unsigned long long)counters.cold_hits.load(), (unsigned long long)counters.paged.load(),
            (unsigned long long)counters.page_hits.load(), (unsigned long long)counters.page_failures.load(),
            (unsigned long long)counters.cold_kept.load(), (unsigned long long)counters.cold_failures.load());
        if (cold_length > 0) Report(cold_line);
        const auto ms = [](const std::atomic<std::uint64_t>& ns) {
            return static_cast<unsigned long long>(ns.load(std::memory_order_relaxed) / 1000000);
        };
        const unsigned long long restores = counters.restores.load(std::memory_order_relaxed);
        const int time_length = std::snprintf(cold_line, sizeof(cold_line),
            "EDEN_CRAM_TIME %s ticks=%u tick_ms=%llu tick_max_ms=%llu compress_ms=%llu write_ms=%llu write_max_ms=%llu "
            "restores=%llu restore_avg_us=%llu restore_max_ms=%llu read_ms=%llu read_max_ms=%llu kept_blocks=%zu "
            "kept_hits=%llu compresses=%llu compress_skips=%llu\n",
            when, cold_tick.load(std::memory_order_relaxed), ms(counters.tick_ns), ms(counters.tick_max_ns),
            ms(counters.compress_ns), ms(counters.write_ns), ms(counters.write_max_ns), restores,
            restores ? (unsigned long long)(counters.restore_ns.load() / 1000 / restores) : 0ull,
            ms(counters.restore_max_ns), ms(counters.read_ns), ms(counters.read_max_ns),
            restore_pool_count.load(std::memory_order_relaxed),
            (unsigned long long)counters.restore_pool_hits.load(std::memory_order_relaxed),
            (unsigned long long)counters.compresses.load(std::memory_order_relaxed),
            (unsigned long long)counters.compress_skips.load(std::memory_order_relaxed));
        if (time_length > 0) Report(cold_line);
    }
#ifdef PS5_NATIVE
    // The pool as well, from this thread: the GPU thread reports it every 20 s (headless/performance.cpp),
    // and a session whose GPU thread stopped showed nothing after that.
    {
        const std::int64_t total = sceKernelGetDirectMemorySize();
        std::int64_t start = 0;
        std::size_t largest = 0;
        if (total > 0 && sceKernelAvailableDirectMemorySize(0, total, 0x4000, &start, &largest) == 0) {
            char pool[160];
            const int pool_length = std::snprintf(pool, sizeof(pool),
                "EDEN_CRAM %s pool largest free block %zu MiB of %lld MiB\n", when, largest >> 20,
                static_cast<long long>(total >> 20));
            if (pool_length > 0) Report(pool);
        }
    }
#endif
    // Who wrote the slots that were promoted: the largest places (offsets in the app's image).
    struct Top { const WriterSite* site; std::uint64_t count; } top[10]{};
    for (const WriterSite& site : writer_sites) {
        const std::uint64_t count = site.count.load(std::memory_order_relaxed);
        if (site.key.load(std::memory_order_relaxed) == 0 || count <= top[9].count) continue;
        std::size_t place = 9;
        while (place > 0 && top[place - 1].count < count) {
            top[place] = top[place - 1];
            --place;
        }
        top[place] = {&site, count};
    }
    char writers[640];
    int used = std::snprintf(writers, sizeof(writers),
                             "EDEN_CRAM_WRITERS promotions by code of the app %llu, by other code %llu\n",
                             (unsigned long long)promotions_in_app_code.load(), (unsigned long long)promotions_elsewhere.load());
    if (used > 0) Report(writers);
    for (const Top& entry : top) {
        if (entry.count == 0) break;
        int length = std::snprintf(writers, sizeof(writers), "EDEN_CRAM_WRITER %llu:", (unsigned long long)entry.count);
        for (const std::uintptr_t pc : entry.site->pcs) {
            if (length > 0 && static_cast<std::size_t>(length) < sizeof(writers) - 24)
                length += std::snprintf(writers + length, sizeof(writers) - length, " %llx",
                                        (unsigned long long)(pc != 0 ? pc - 0x400000ull : 0));
        }
        if (length > 0 && static_cast<std::size_t>(length) < sizeof(writers) - 2) {
            writers[length++] = '\n';
            writers[length] = 0;
            Report(writers);
        }
    }
}

// What finer slots would give: 16 KiB pages of the Own slots that hold only zeroes, and those that
// repeat another page (by a full hash). Read once, when the session ends.
void MeasureFinePages(SparseRange& range) noexcept {
    constexpr std::size_t Fine = 16384;
    const std::size_t slots = (range.end - range.begin.load(std::memory_order_relaxed)) / SparseSlot;
    std::vector<std::uint64_t> hashes;
    std::size_t pages = 0, zero = 0;
    for (std::size_t slot = 0; slot < slots; ++slot) {
        if (SlotState(range, slot) != SlotOwn) continue;
        for (std::size_t offset = 0; offset < SparseSlot; offset += Fine) {
            const std::uintptr_t at = SlotAddress(range, slot) + offset;
            ++pages;
            const auto* word = reinterpret_cast<const volatile std::uint64_t*>(at);
            bool is_zero = true;
            for (std::size_t i = 0; i < Fine / 8 && is_zero; ++i) is_zero = word[i] == 0;
            if (is_zero) ++zero;
            else hashes.push_back(FullHash(at, Fine));
        }
    }
    std::sort(hashes.begin(), hashes.end());
    std::size_t repeats = 0;
    for (std::size_t i = 1; i < hashes.size(); ++i) repeats += hashes[i] == hashes[i - 1];
    char line[192];
    const int length = std::snprintf(line, sizeof(line),
        "EDEN_CRAM_FINE pages16k=%zu zero=%zu (%zuMiB) repeats=%zu (%zuMiB)\n", pages, zero, zero * Fine >> 20, repeats,
        repeats * Fine >> 20);
    if (length > 0) Report(line);
}

void ScannerLoop(SparseRange* range) {
    auto next_report = std::chrono::steady_clock::now() + std::chrono::seconds(30);
    std::unique_lock lock{scanner_mutex};
    unsigned wakes = 0;
    while (!scanner_stop) {
        scanner_wake.wait_for(lock, std::chrono::seconds(ColdTickSeconds), [] { return scanner_stop; });
        if (scanner_stop) break;
        lock.unlock();
#ifdef PS5_NATIVE
        ColdTick(*range);
#endif
        if (++wakes % 4 == 0 && scan_requested.load(std::memory_order_relaxed)) ScanPass(*range);
        if (std::chrono::steady_clock::now() >= next_report) {
            ServiceReport(*range, "now");
            next_report = std::chrono::steady_clock::now() + std::chrono::seconds(30);
        }
        lock.lock();
    }
}

// Once: does a write to a slot that shows the zero block come back here, with its address, and
// resume? A refusal keeps the guest's memory committed when it is handed out, as before.
bool ServiceAvailable() noexcept {
    int state = service_state.load(std::memory_order_acquire);
    if (state != 0) return state == 1;
    const char* step = "ok";
    long long fault_ns = -1;
    void* base = nullptr;
    if (!SparseAvailable()) step = "sparse";
    else if (!InstallServiceSignal()) step = "install";
    else if ((base = NewSparseRange(2 * SparseSlot, false)) == nullptr) step = "reserve";
    if (base != nullptr) {
        SparseRange* range = nullptr;
        {
            const std::lock_guard lock{sparse_mutex};
            range = SparseRangeOf(reinterpret_cast<std::uintptr_t>(base));
        }
        if (range == nullptr || !AddStates(*range)) {
            step = "states";
        } else {
            probe_range.store(range, std::memory_order_release);
            auto* word = reinterpret_cast<volatile std::uint64_t*>(static_cast<std::uint8_t*>(base) + SparseSlot + 64);
            const auto before = counters.faults.load(std::memory_order_relaxed);
            probe_expected.store(reinterpret_cast<std::uintptr_t>(word), std::memory_order_release);
            const auto start = std::chrono::steady_clock::now();
            *word = 0x5a17ed5a17ed5a17ull; // faults once: the slot shows the zero block
            fault_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - start).count();
            probe_expected.store(0, std::memory_order_release);
            if (*word != 0x5a17ed5a17ed5a17ull) step = "value";
            else if (counters.faults.load(std::memory_order_relaxed) != before + 1) step = "fault-count";
            else if (probe_mismatch.load(std::memory_order_relaxed)) step = "fault-address";
            else if (SlotState(*range, 1) != SlotOwn || SlotState(*range, 0) != SlotZero) step = "state";
            else {
                // Back to the zero block, then written again: a slot comes and goes.
                LockSlot(*range, 1);
                ReturnSlot(*range, 1);
                UnlockSlot(*range, 1, SlotZero);
                if (*word != 0) step = "return";
                else {
                    *word = 7;
                    if (*word != 7 || SlotState(*range, 1) != SlotOwn) step = "again";
                }
            }
            if (std::strcmp(step, "ok") == 0) {
                // Fenced and unfenced: read while fenced, written after.
                const std::uintptr_t at = SlotAddress(*range, 1);
                Fence(at);
                if (*word != 7) step = "fence-read";
                Unfence(at);
                *word = 9;
                if (std::strcmp(step, "ok") == 0 && *word != 9) step = "unfence";
            }
            if (std::strcmp(step, "ok") == 0) {
                // Two slots of the same bytes: one block, then copied apart by a write, then the last
                // one takes the block back.
                auto* other = reinterpret_cast<volatile std::uint64_t*>(static_cast<std::uint8_t*>(base) + 64);
                *other = 9;
                const bool shared = ScanShare(*range, 1, 0);
#ifdef PS5_NATIVE
                if (!shared || SlotState(*range, 0) != SlotShared || SlotState(*range, 1) != SlotShared) step = "share";
                else if (*other != 9 || *word != 9) step = "share-read";
                else {
                    *other = 10; // a copy of its own
                    if (*other != 10 || *word != 9 || SlotState(*range, 0) != SlotOwn) step = "share-copy";
                    else {
                        *word = 11; // the last one: the block is its own again
                        if (*word != 11 || *other != 10 || SlotState(*range, 1) != SlotOwn) step = "share-last";
                    }
                }
#else
                if (shared) step = "share-host";
#endif
            }
            if (std::strcmp(step, "ok") == 0) {
                // Slot 1 with one page of data at page 3: Fine (one 16 KiB page kept), read in place,
                // then a write makes it whole again with every byte where it was.
                LockSlot(*range, 1);
                if (SlotState(*range, 1) != SlotZero) ReturnSlot(*range, 1);
                UnlockSlot(*range, 1, SlotZero);
                auto* page3 = reinterpret_cast<volatile std::uint64_t*>(static_cast<std::uint8_t*>(base) + SparseSlot + 3 * FinePage + 8);
                *page3 = 0x0f1e2d3c4b5a6978ull;
                const auto held = guest_committed.load(std::memory_order_relaxed);
                if (!ScanFine(*range, 1) || SlotState(*range, 1) != SlotFine) step = "fine";
                else if (*page3 != 0x0f1e2d3c4b5a6978ull || *word != 0) step = "fine-read";
                else if (held - guest_committed.load(std::memory_order_relaxed) != SparseSlot - FinePage) step = "fine-held";
                else {
                    *word = 13; // page 0: the slot is whole again
                    if (*word != 13 || *page3 != 0x0f1e2d3c4b5a6978ull || SlotState(*range, 1) != SlotOwn ||
                        (range->fine[1] & 0xf00) == 0)
                        step = "fine-write";
                }
            }
            for (std::size_t slot = 0; slot < 2; ++slot)
                if (SlotState(*range, slot) != SlotZero) {
                    LockSlot(*range, slot);
                    ReturnSlot(*range, slot);
                    UnlockSlot(*range, slot, SlotZero);
                }
            probe_range.store(nullptr, std::memory_order_release);
        }
        FreeSparsePages(base, 2 * SparseSlot);
    }
    ResetCounters();
    const bool ok = std::strcmp(step, "ok") == 0;
    service_refused = step;
    char line[128];
    const int length = std::snprintf(line, sizeof(line), "EDEN_CRAM_CHECK ok=%d step=%s fault_ns=%lld\n", ok ? 1 : 0,
                                     step, fault_ns);
    if (length > 0) Report(line);
    service_state.store(ok ? 1 : 2, std::memory_order_release);
    return ok;
}

// The guest range starts being a service (when the platform passed the check).
void ServiceStart(SparseRange& range) noexcept {
    // A refusal belongs to the session it ended: a later crash must not report it again.
    refused_address.store(0, std::memory_order_relaxed);
    if (!service_requested.load(std::memory_order_relaxed) || !ServiceAvailable() || !AddStates(range)) {
        char line[96];
        const int length = std::snprintf(line, sizeof(line), "EDEN_CRAM off: %s\n",
                                         !service_requested.load() ? "switched-off" : service_refused);
        if (length > 0) Report(line);
        return;
    }
    ResetCounters();
    // Watch and Cold (the console): a record a slot.
    cold_slot_count = (range.end - range.begin.load(std::memory_order_relaxed)) / SparseSlot;
    cold_slots = new (std::nothrow) ColdSlot[cold_slot_count];
    if (cold_slots == nullptr) cold_slot_count = 0;
    cold_tick.store(0, std::memory_order_relaxed);
#ifdef PS5_NATIVE
    if (page_fd < 0) {
        bool opened = true;
        for (std::size_t file = 0; file < PageFiles && opened; ++file) {
            char path[96];
            std::snprintf(path, sizeof(path), PageFilePath, unsigned(file));
            page_fds[file] = open(path, O_RDWR | O_CREAT | O_TRUNC, 0666);
            opened = page_fds[file] >= 0;
        }
        if (opened) {
            page_fd = page_fds[0];
        } else {
            for (int& fd : page_fds)
                if (fd >= 0) (void)close(fd), fd = -1;
            Note("EDEN_CRAM no page file: slots that do not compress stay in memory\n");
        }
    }
    page_place_used = new (std::nothrow) std::atomic<std::uint8_t>[cold_slot_count]();
    if (page_place_used == nullptr && page_fd >= 0) {
        for (int& fd : page_fds)
            if (fd >= 0) (void)close(fd), fd = -1;
        page_fd = -1;
    }
#endif
    range.promote = true;
#ifdef PS5_NATIVE
    reserve_lent.store(0, std::memory_order_relaxed);
    FillReserve(ReserveAtStart);
#endif
    service_range.store(&range, std::memory_order_release);
    Eden::Fastmem::guest_write_fault.store(&HandleGuestWriteFault, std::memory_order_release);
#ifdef PS5_NATIVE
    HideZeroSlots(range); // with the fault handler in place
#endif
    {
        const std::lock_guard lock{scanner_mutex};
        scanner_stop = false;
    }
    scanner = std::thread(ScannerLoop, &range);
#ifdef PS5_NATIVE
    keeper = std::thread(ReserveKeeper);
#endif
    char line[128];
    const int length = std::snprintf(line, sizeof(line), "EDEN_CRAM on: scan=%d dedup=%d fine=%d reserve=%zuMiB\n",
                                     scan_requested.load() ? 1 : 0, dedup_requested.load() ? 1 : 0,
                                     fine_requested.load() ? 1 : 0,
                                     reserve_count.load() * (SparseSlot >> 20));
    if (length > 0) Report(line);
}

void ServiceStop(SparseRange& range) noexcept {
    if (service_range.load(std::memory_order_acquire) != &range) return;
    {
        const std::lock_guard lock{scanner_mutex};
        scanner_stop = true;
    }
    scanner_wake.notify_all();
    if (scanner.joinable()) scanner.join();
#ifdef PS5_NATIVE
    if (keeper.joinable()) keeper.join();
#endif
    ServiceReport(range, "end");
    MeasureFinePages(range);
    service_range.store(nullptr, std::memory_order_release);
#ifdef PS5_NATIVE
    EmptyReserve();
#endif
}

// What a guest range keeps when it is given back: each block once, shared ones too.
void ReleaseServiceBlocks(SparseRange& range) noexcept {
#ifdef PS5_NATIVE
    const std::size_t slots = (range.end - range.begin.load(std::memory_order_relaxed)) / SparseSlot;
    for (std::size_t slot = 0; slot < slots; ++slot) {
        const auto state = SlotState(range, slot);
        if (state == SlotFine) {
            const std::size_t kept = FineKept(range, slot);
            if (kept != 0 && sceKernelReleaseDirectMemory(range.physical[slot], kept * FinePage) != 0) std::abort();
            guest_committed.fetch_sub(kept * FinePage, std::memory_order_relaxed);
        } else if (state == SlotOwn || state == SlotWatch) {
            if (sceKernelReleaseDirectMemory(range.physical[slot], SparseSlot) != 0) std::abort();
        } else if (state == SlotCold) {
            if (cold_slots != nullptr && slot < cold_slot_count) {
                delete cold_slots[slot].data;
                cold_slots[slot].data = nullptr;
            }
        } else if (state == SlotShared && UnshareBlock(range.physical[slot]) == 0) {
            if (sceKernelReleaseDirectMemory(range.physical[slot], SparseSlot) != 0) std::abort();
            guest_committed.fetch_sub(SparseSlot, std::memory_order_relaxed);
        }
        if (state == SlotOwn || state == SlotWatch) guest_committed.fetch_sub(SparseSlot, std::memory_order_relaxed);
        range.owned[slot].store(false, std::memory_order_relaxed);
    }
    if (cold_slots != nullptr) {
        for (std::size_t slot = 0; slot < cold_slot_count; ++slot) delete cold_slots[slot].retired.exchange(nullptr);
        delete[] cold_slots;
        cold_slots = nullptr;
        cold_slot_count = 0;
        counters.cold_bytes.store(0, std::memory_order_relaxed);
    }
    if (page_fd >= 0) {
        for (std::size_t file = 0; file < PageFiles; ++file) {
            (void)close(page_fds[file]);
            page_fds[file] = -1;
            char path[96];
            std::snprintf(path, sizeof(path), PageFilePath, unsigned(file));
            (void)unlink(path);
        }
        page_fd = -1;
    }
    delete[] page_place_used;
    page_place_used = nullptr;
    while (restore_pool_count.load(std::memory_order_relaxed) > 0) {
        const std::size_t count = restore_pool_count.load(std::memory_order_relaxed);
        if (sceKernelReleaseDirectMemory(restore_pool[count - 1], SparseSlot) != 0) std::abort();
        restore_pool_count.store(count - 1, std::memory_order_relaxed);
    }
#else
    const std::size_t slots = (range.end - range.begin.load(std::memory_order_relaxed)) / SparseSlot;
    for (std::size_t slot = 0; slot < slots; ++slot) {
        if (SlotState(range, slot) == SlotOwn) guest_committed.fetch_sub(SparseSlot, std::memory_order_relaxed);
        if (SlotState(range, slot) == SlotFine) guest_committed.fetch_sub(FineKept(range, slot) * FinePage, std::memory_order_relaxed);
        range.owned[slot].store(false, std::memory_order_relaxed);
    }
#endif
}
} // namespace

// ---- What the guest's kernel tells the service ----

// Development switches (dev-settings cram=off, cram_scan=off, cram_dedup=off), before the guest's
// memory is taken.
void SetGuestMemoryService(bool on, bool scan, bool dedup) noexcept {
    service_requested.store(on, std::memory_order_relaxed);
    scan_requested.store(scan, std::memory_order_relaxed);
    dedup_requested.store(dedup, std::memory_order_relaxed);
}

// Settings > Performance > Compact memory (off unless chosen): written slots with many pages of
// zeroes keep only the others. It gives back more memory, and a game that writes such a slot again
// waits for it to be made whole (a fault and a 2 MiB copy, about 0.3 ms).
void SetGuestMemoryFine(bool on) noexcept {
    fine_requested.store(on, std::memory_order_relaxed);
}

// The guest's kernel hands pages out: they are committed now only without the service.
void CommitGuestMemoryOnHandOut(std::uint64_t offset, std::size_t bytes) noexcept {
    SparseRange* const range = guest_range.load(std::memory_order_acquire);
    if (range != nullptr && range->promote) return;
    CommitGuestMemory(offset, bytes);
}

// The guest's kernel fills pages (`at` is the backing at `offset`). With the service, a fill with
// zeroes leaves Zero slots alone and gives whole Own or Shared slots back; anything else is written.
void ClearGuestMemory(std::uint8_t* at, std::uint64_t offset, std::size_t bytes, int fill) noexcept {
    SparseRange* const range = guest_range.load(std::memory_order_acquire);
    if (range == nullptr || !range->promote || fill != 0) {
        if (range != nullptr && !range->promote) CommitGuestMemory(offset, bytes);
        std::memset(at, fill, bytes); // (with the service, a write to a Zero slot promotes it)
        return;
    }
    const std::uint64_t end = offset + bytes;
    for (std::uint64_t cursor = offset; cursor < end;) {
        const std::size_t slot = static_cast<std::size_t>(cursor / SparseSlot);
        const std::uint64_t slot_end = (cursor / SparseSlot + 1) * SparseSlot;
        const std::uint64_t piece_end = std::min(end, slot_end);
        const bool whole = cursor % SparseSlot == 0 && piece_end == slot_end;
        if (whole) {
            LockSlot(*range, slot);
            const auto state = SlotState(*range, slot);
            if (state != SlotZero) {
                ReturnSlot(*range, slot);
                counters.returns_clear.fetch_add(1, std::memory_order_relaxed);
            }
            UnlockSlot(*range, slot, SlotZero);
        } else if (SlotState(*range, slot) != SlotZero) {
            std::memset(at + (cursor - offset), 0, static_cast<std::size_t>(piece_end - cursor));
        }
        cursor = piece_end;
    }
}

// A page the guest's kernel fills one at a time (optimized allocation): the same rule.
void FillGuestMemory(std::uint8_t* at, int fill, std::size_t bytes) noexcept {
    SparseRange* const range = guest_range.load(std::memory_order_acquire);
    if (range != nullptr && range->promote && fill == 0) {
        const auto begin = range->begin.load(std::memory_order_relaxed);
        const auto address = reinterpret_cast<std::uintptr_t>(at);
        if (address >= begin && address < range->end && SlotState(*range, (address - begin) / SparseSlot) == SlotZero)
            return;
    }
    std::memset(at, fill, bytes);
}

// The guest's kernel freed pages: every slot they cover whole goes back.
void GuestMemoryFreed(std::uint64_t offset, std::size_t bytes) noexcept {
    SparseRange* const range = guest_range.load(std::memory_order_acquire);
    if (range == nullptr || !range->promote || bytes < SparseSlot) return;
    const std::uint64_t first = (offset + SparseSlot - 1) / SparseSlot;
    const std::uint64_t last = (offset + bytes) / SparseSlot; // exclusive
    for (std::uint64_t slot = first; slot < last; ++slot) {
        LockSlot(*range, static_cast<std::size_t>(slot));
        if (SlotState(*range, static_cast<std::size_t>(slot)) != SlotZero) {
            ReturnSlot(*range, static_cast<std::size_t>(slot));
            counters.returns_free.fetch_add(1, std::memory_order_relaxed);
        }
        UnlockSlot(*range, static_cast<std::size_t>(slot), SlotZero);
    }
}

// For the host checks: one scan pass now, and a slot's state (0 Zero, 1 Own, 2 Shared, -1 none).
void GuestMemoryScanNow() noexcept {
    if (SparseRange* const range = service_range.load(std::memory_order_acquire)) ScanPass(*range);
}
int GuestMemorySlotState(std::uint64_t offset) noexcept {
    SparseRange* const range = guest_range.load(std::memory_order_acquire);
    if (range == nullptr || range->state == nullptr) return -1;
    return SlotState(*range, static_cast<std::size_t>(offset / SparseSlot));
}
