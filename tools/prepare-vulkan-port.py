"""Adapt the pinned Eden Vulkan platform boundary and required renderer fixes."""
from pathlib import Path
import struct
import subprocess
import sys
# --report: name every anchor that no longer matches (make update), instead of stopping at the first.
report = '--report' in sys.argv
source, output = map(Path, [a for a in sys.argv[1:] if a != '--report'])
port = Path(__file__).resolve().parents[1] / 'headless'
broken = []
def adapt(relative, destination, replacements):
    text = (source / relative).read_text()
    if relative.endswith('/vk_query_cache.cpp'):
        # The core traces each host conditional rendering decision; the port counts them itself
        # (hcr counters below), so drop those calls and keep the port's anchors on the logic.
        import re
        text = re.sub(r'\n[ \t]*TraceConditionalRendering\([^;]*?\);(?=\n)', '', text)
    for old, new in replacements:
        if relative.endswith('/vk_rasterizer.cpp') and old.endswith('// update engine as channel may be different.') and old not in text:
            marker = '// Donkey Kong Bananza\'s terrain:'
            if marker in text:
                old = old.replace('// update engine as channel may be different.', marker)
                new = new.replace('// update engine as channel may be different.', marker)
        if relative.endswith('/vulkan_memory_allocator.cpp') and old.startswith('    vk::Check(vmaCreateBuffer(') and old not in text:
            if 'VkResult result = vmaCreateBuffer(' in text:
                # This core already retries outside the driver budget on allocation failure.
                if 'host_ci.flags &= ~VMA_ALLOCATION_CREATE_WITHIN_BUDGET_BIT;' in text:
                    continue
        if text.count(old) != 1:
            if report:
                broken.append(f"{relative}: {old[:70]!r} (matches {text.count(old)})")
                continue
            raise RuntimeError(f"Pinned Vulkan anchor changed: {relative}: {old[:60]}")
        text = text.replace(old, new)
    path = output / destination
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)

adapt('src/core/frontend/emu_window.h', 'include/core/frontend/emu_window.h',
      [('    Xcb,', '    Xcb,\n    PS5,')])
adapt('src/video_core/renderer_vulkan/vk_rasterizer.cpp', 'vulkan_rasterizer.cpp', [
    # Compute synchronization (dev_vulkan.h compute_barriers): indirect
    # dispatches returned before the pre-dispatch barrier, and no barrier made a dispatch's
    # writes visible to later indirect arguments, vertex/index fetch, shaders or copies.
    ('void RasterizerVulkan::DispatchCompute() {',
     """namespace {
constexpr VkMemoryBarrier COMPUTE_WRITE_BARRIER{
    .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
    .pNext = nullptr,
    .srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT,
    .dstAccessMask = VK_ACCESS_INDIRECT_COMMAND_READ_BIT | VK_ACCESS_INDEX_READ_BIT |
                     VK_ACCESS_VERTEX_ATTRIBUTE_READ_BIT | VK_ACCESS_UNIFORM_READ_BIT |
                     VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT |
                     VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT,
};
constexpr VkMemoryBarrier COMPUTE_INPUT_BARRIER{
    .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
    .pNext = nullptr,
    .srcAccessMask = VK_ACCESS_MEMORY_WRITE_BIT,
    .dstAccessMask = VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_INDIRECT_COMMAND_READ_BIT,
};
constexpr VkPipelineStageFlags COMPUTE_CONSUMER_STAGES =
    VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT | VK_PIPELINE_STAGE_VERTEX_INPUT_BIT |
    VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT |
    VK_PIPELINE_STAGE_TRANSFER_BIT;
} // namespace

void RasterizerVulkan::DispatchCompute() {"""),
    ('        scheduler.RequestOutsideRenderPassOperationContext();\n        record_read_barrier();\n        scheduler.TraceComputePipeline(pipeline->ShaderHash());\n        scheduler.Record([this, pipeline, indirect_buffer = buffer->Handle(),',
     """        scheduler.RequestOutsideRenderPassOperationContext();
        record_read_barrier();
        scheduler.TraceComputePipeline(pipeline->ShaderHash());
        if (::Eden::DevVulkan::compute_barriers) {
            scheduler.Record([](vk::CommandBuffer cmdbuf) {
                cmdbuf.PipelineBarrier(vk::PIPELINE_STAGE_GRAPHICS_COMPUTE_TRANSFER,
                                       VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT | VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT,
                                       0, COMPUTE_INPUT_BARRIER);
            });
        }
        scheduler.Record([this, pipeline, indirect_buffer = buffer->Handle(),"""),
    ('                cmdbuf.DispatchIndirect(indirect_buffer, indirect_offset);\n            });\n',
     """                cmdbuf.DispatchIndirect(indirect_buffer, indirect_offset);
            });
            if (::Eden::DevVulkan::compute_barriers)
                cmdbuf.PipelineBarrier(VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, COMPUTE_CONSUMER_STAGES, 0,
                                       COMPUTE_WRITE_BARRIER);
"""),
    ('                               [&] { cmdbuf.Dispatch(dim[0], dim[1], dim[2]); });\n',
     """                               [&] { cmdbuf.Dispatch(dim[0], dim[1], dim[2]); });
        if (::Eden::DevVulkan::compute_barriers)
            cmdbuf.PipelineBarrier(VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, COMPUTE_CONSUMER_STAGES, 0,
                                   COMPUTE_WRITE_BARRIER);
"""),
    # CounterEnable below starts a query immediately. A deferred clear has no
    # active pass yet, so later realization would end that query in a different
    # render-pass scope (VUID-vkCmdEndQuery-None-07007). Keep the immediate path
    # when the guest enables occlusion counting.
    ('const bool can_defer_clear = ENABLE_DEFERRED_CLEAR &&',
     'const bool can_defer_clear = ENABLE_DEFERRED_CLEAR && !regs.zpass_pixel_count_enable &&'),
    ('#include <algorithm>', '#include <algorithm>\n#include "performance.h"'),
    # Guest threads take the cache locks here (tracked-page writes, flush-area reads).
    ('bool RasterizerVulkan::OnCPUWrite(DAddr addr, u64 size) {',
     'bool RasterizerVulkan::OnCPUWrite(DAddr addr, u64 size) {\n    ::Eden::Performance::Timer guest_write_timer(::Eden::Performance::guest_cpu_write);'),
    ('VideoCore::RasterizerDownloadArea RasterizerVulkan::GetFlushArea(DAddr addr, u64 size) {',
     'VideoCore::RasterizerDownloadArea RasterizerVulkan::GetFlushArea(DAddr addr, u64 size) {\n    ::Eden::Performance::Timer guest_read_timer(::Eden::Performance::guest_cpu_read);'),
    # ... and wait for those locks spinning briefly before sleeping (performance.h GuestCacheLock).
    ('        std::scoped_lock lock{texture_cache.mutex};\n        auto area = texture_cache.GetFlushArea(addr, size);',
     '        ::Eden::Performance::GuestCacheLock(texture_cache.mutex);\n'
     '        std::lock_guard lock{texture_cache.mutex, std::adopt_lock};\n        auto area = texture_cache.GetFlushArea(addr, size);'),
    ('        std::scoped_lock lock{buffer_cache.mutex};\n        if (buffer_cache.OnCPUWrite(addr, size)) {',
     '        ::Eden::Performance::GuestCacheLock(buffer_cache.mutex);\n'
     '        std::lock_guard lock{buffer_cache.mutex, std::adopt_lock};\n        if (buffer_cache.OnCPUWrite(addr, size)) {'),
    ('        std::scoped_lock lock{texture_cache.mutex};\n        texture_cache.WriteMemory(addr, size);\n    }\n    pipeline_cache.InvalidateRegion(addr, size);',
     '        ::Eden::Performance::GuestCacheLock(texture_cache.mutex);\n'
     '        std::lock_guard lock{texture_cache.mutex, std::adopt_lock};\n        texture_cache.WriteMemory(addr, size);\n    }\n    pipeline_cache.InvalidateRegion(addr, size);'),
    # 512 draws per command buffer submission for titles without native tiling (Switch 1):
    # 4,096 exceeds the PS5 GPU watchdog timeout in shader-heavy scenes (VK_ERROR_DEVICE_LOST in
    # Xenoblade 2's cities). Native (Switch 2) titles already submit every 128 draws
    # (SubmissionPolicy::DefaultNativeBudget).
    ('    u32 legacy_budget = 4096;\n', '    u32 legacy_budget = 512;\n'),
    ('(device.IsMoltenVK() ? 128U : 4096U);', '(device.IsMoltenVK() ? 128U : 512U);'),
    # Where the ~4 us a draw spends goes: six phases under one running clock, each Mark taking the
    # gap since the previous one so they do not overlap. The Timer still wraps the whole function,
    # so a draw that returns early is charged for its unmarked tail and the sum of the six falls
    # short by that amount plus two clock reads per phase. control/scheduler.cpp aside, this is the
    # only place the dispatch's time can be spent - so its six numbers should add up to it.
    ('''    namespace Profile = VideoCommon::Nx2Profile;
    {
        Profile::Scope scope{Profile::Zone::FlushWork};
        FlushWork();
        gpu_memory->FlushCaching();
    }
''',
     '''    namespace Profile = VideoCommon::Nx2Profile;
    const ::Eden::Performance::Timer rasterizer_draw_timer(::Eden::Performance::rasterizer_draw);
    auto phase = ::Eden::Performance::MarkStart();
    {
        Profile::Scope scope{Profile::Zone::FlushWork};
        FlushWork();
        gpu_memory->FlushCaching();
    }
    ::Eden::Performance::Mark(::Eden::Performance::draw_flush, phase);
'''),
    ('''        return pipeline_cache.CurrentGraphicsPipeline();
    }()};
''',
     '''        return pipeline_cache.CurrentGraphicsPipeline();
    }()};
    ::Eden::Performance::Mark(::Eden::Performance::draw_pipeline, phase);
'''),
    ('    pipeline->SetEngine(maxwell3d, gpu_memory);\n    const u32 repack_dirty',
     '    pipeline->SetEngine(maxwell3d, gpu_memory);\n'
     '    // Waiting for both cache mutexes. SetEngine between them and here is two pointer assignments,\n'
     '    // and the hold itself carries on through what follows: the phases below measure it.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::draw_lock_wait, phase);\n'
     '    const u32 repack_dirty'),
    ('        TraceRenderState(is_indexed, "configure-failed", nullptr);\n        return;\n    }\n',
     '        TraceRenderState(is_indexed, "configure-failed", nullptr);\n        return;\n    }\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::draw_config, phase);\n'),
    ('    query_cache.CounterEnable(VideoCommon::QueryType::ZPassPixelCount64, maxwell3d->regs.zpass_pixel_count_enable);\n'
     '    {\n        Profile::Scope scope{Profile::Zone::DrawRecord};\n',
     '    query_cache.CounterEnable(VideoCommon::QueryType::ZPassPixelCount64, maxwell3d->regs.zpass_pixel_count_enable);\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::draw_state, phase);\n'
     '    {\n        Profile::Scope scope{Profile::Zone::DrawRecord};\n'),
    ('        draw_func();\n    }\n    submitted = true;',
     '        draw_func();\n    }\n    ::Eden::Performance::Mark(::Eden::Performance::draw_record, phase);\n    submitted = true;'),
    # A forced release blocks the GPU thread until the fence thread has retired
    # every older fence. Time only that producer-side drain.
    ('    fence_manager.WaitPendingFences(force);',
     '''    if (force) {
        ::Eden::Performance::Timer drain_timer(::Eden::Performance::gpu_fence_drain);
        fence_manager.WaitPendingFences(force);
    } else {
        fence_manager.WaitPendingFences(force);
    }'''),
    # UpdateDynamicStates asked the cache for the current pipeline a second time
    # per draw, recomputing the same fixed-state key. Reuse PrepareDraw's result;
    # other callers keep the original lookup.
    ('template <typename Func>\nvoid RasterizerVulkan::PrepareDraw(bool is_indexed, Func&& draw_func, bool is_indirect) {',
     '''namespace {
// GPU thread only: the pipeline PrepareDraw refreshed for the draw in progress.
GraphicsPipeline* prepared_pipeline = nullptr;
} // namespace

template <typename Func>
void RasterizerVulkan::PrepareDraw(bool is_indexed, Func&& draw_func, bool is_indirect) {'''),
    ('    std::scoped_lock lock{buffer_cache.mutex, texture_cache.mutex};\n    // update engine as channel may be different.',
     '''    std::scoped_lock lock{buffer_cache.mutex, texture_cache.mutex};
    prepared_pipeline = pipeline;
    SCOPE_EXIT {
        prepared_pipeline = nullptr;
    };
    // update engine as channel may be different.'''),
    # Development GPU fault probe (dev_vulkan.h, pipeline_trace=on): once the runner
    # creates /app0/sync-draws.txt, report every draw/dispatch to klog and wait for the
    # GPU, so the last report before a fatal GPU fault names the operation.
    ('#include "performance.h"', '#include "performance.h"\n#include "dev_vulkan.h"\n#include "diagnostics.h"\n#include <filesystem>'),
    ('GraphicsPipeline* prepared_pipeline = nullptr;',
     'GraphicsPipeline* prepared_pipeline = nullptr;\n'
     '// The pipeline of the last prepared draw, for the development fault probe.\n'
     'GraphicsPipeline* probe_pipeline = nullptr;\n'
     'bool SyncProbeActive() {\n'
     '    static unsigned poll = 0;\n'
     '    static bool active = false;\n'
     '    if (!active && ::Eden::DevVulkan::trace_pipelines && (++poll & 255) == 0)\n'
     '        active = std::filesystem::exists("/app0/sync-draws.txt");\n'
     '    return active;\n'
     '}\n'
     'void SyncProbe(Scheduler& scheduler, const char* kind, const void* pipeline) {\n'
     '    if (!SyncProbeActive()) return;\n'
     '    static unsigned long long sequence = 0;\n'
     '    char text[96];\n'
     '    std::snprintf(text, sizeof(text), "%s #%llu p=%p", kind, ++sequence, pipeline);\n'
     '    ::Eden::Report("sync", text);\n'
     '    scheduler.Finish();\n'
     '}'),
    ('    prepared_pipeline = pipeline;\n', '    prepared_pipeline = pipeline;\n    probe_pipeline = pipeline;\n'),
    ('        }\n    });\n}\n\nvoid RasterizerVulkan::DrawIndirect() {',
     '        }\n    });\n    SyncProbe(scheduler, "draw", probe_pipeline);\n}\n\nvoid RasterizerVulkan::DrawIndirect() {'),
    ('void RasterizerVulkan::DrawTexture() {', 'void RasterizerVulkan::DrawTexture() {\n    SCOPE_EXIT { SyncProbe(scheduler, "draw_texture", nullptr); };'),
    ('void RasterizerVulkan::Clear(u32 layer_count) {', 'void RasterizerVulkan::Clear(u32 layer_count) {\n    SCOPE_EXIT { SyncProbe(scheduler, "clear", nullptr); };'),
    ('    ComputePipeline* const pipeline{pipeline_cache.CurrentComputePipeline()};\n    if (!pipeline) {\n        if (kepler_compute->IsNativeMode()) throw Shader::RuntimeError("Native compute pipeline unavailable");\n        return;\n    }',
     '    ComputePipeline* const pipeline{pipeline_cache.CurrentComputePipeline()};\n    if (!pipeline) {\n        if (kepler_compute->IsNativeMode()) throw Shader::RuntimeError("Native compute pipeline unavailable");\n        return;\n    }\n'
     '    SCOPE_EXIT { SyncProbe(scheduler, "dispatch", pipeline); };'),
])
# Host conditional rendering (VK_EXT_conditional_rendering, dev_vulkan.h hcr_mode): count each
# decision (EDEN_DEV_GUEST cond= slots 13-19); hcr=exact sends the conditions Eden would draw
# unconditionally, or predicate from a pending host query, to the exact CPU evaluation;
# hcr=cpu evaluates every condition on the CPU and predicates from constant 0/1 buffers.
adapt('src/video_core/renderer_vulkan/vk_query_cache.cpp', 'vulkan_query_cache.cpp', [
    ('#include "video_core/vulkan_common/vulkan_wrapper.h"\n',
     '#include "video_core/vulkan_common/vulkan_wrapper.h"\n#include "dev_vulkan.h"\n#include "performance.h"\n'),
    ('    vk::Buffer hcr_resolve_buffer;\n',
     '    vk::Buffer hcr_resolve_buffer;\n    vk::Buffer hcr_constant_buffer; // hcr=cpu: predicates 0 and 1\n'),
    ('        hcr_resolve_buffer = memory_allocator.CreateBuffer(buffer_ci, MemoryUsage::DeviceLocal);\n    }\n',
     '''        hcr_resolve_buffer = memory_allocator.CreateBuffer(buffer_ci, MemoryUsage::DeviceLocal);
        if (device.IsExtConditionalRendering() && ::Eden::DevVulkan::hcr_mode == 2) {
            VkBufferCreateInfo constant_ci = buffer_ci;
            constant_ci.size = 2 * sizeof(u32);
            constant_ci.usage = VK_BUFFER_USAGE_CONDITIONAL_RENDERING_BIT_EXT;
            hcr_constant_buffer = memory_allocator.CreateBuffer(constant_ci, MemoryUsage::Upload);
            const u32 predicates[2]{0, 1};
            std::memcpy(hcr_constant_buffer.Mapped().data(), predicates, sizeof(predicates));
            hcr_constant_buffer.Flush();
        }
    }
'''),
    ('''bool QueryCacheRuntime::HostConditionalRenderingCompareValue(VideoCommon::LookupData object_1,
                                                             [[maybe_unused]] bool qc_dirty) {
    if (!impl->device.IsExtConditionalRendering()) {
        return false;
    }
    HostConditionalRenderingCompareBCImpl(object_1.address, true, true);
    return true;
}''',
     '''namespace {
// hcr=cpu: Maxwell3D::ProcessQueryCondition's exact evaluation (the read flushes pending GPU
// results), then the driver predicates the draws from a constant buffer. kind: 0 Conditional,
// 1 IfEqual, 2 IfNotEqual.
bool HostConditionalRenderingOnCpu(QueryCacheRuntime& runtime, QueryCacheRuntimeImpl& impl, DAddr address,
                                   unsigned kind) {
    if (address == 0 || !impl.hcr_constant_buffer) {
        ::Eden::Performance::CountCondition(13);
        runtime.EndHostConditionalRendering();
        return false;
    }
    u32 compare[6]{};
    impl.device_memory.ReadBlock(address, compare, sizeof(compare));
    const bool equal = compare[0] == compare[4] && compare[1] == compare[5];
    const bool execute = kind == 0 ? compare[0] != 0 && compare[1] != 0 : (kind == 1 ? equal : !equal);
    ::Eden::Performance::CountCondition(execute ? 17 : 18);
    const bool was_running = impl.is_hcr_running;
    if (was_running) {
        runtime.PauseHostConditionalRendering();
    }
    impl.hcr_setup.buffer = *impl.hcr_constant_buffer;
    impl.hcr_setup.offset = execute ? sizeof(u32) : 0;
    impl.hcr_setup.flags = 0;
    impl.hcr_is_set = true;
    impl.is_hcr_running = false;
    if (was_running) {
        runtime.ResumeHostConditionalRendering();
    }
    return true;
}
} // namespace

bool QueryCacheRuntime::HostConditionalRenderingCompareValue(VideoCommon::LookupData object_1,
                                                             [[maybe_unused]] bool qc_dirty) {
    if (!impl->device.IsExtConditionalRendering()) {
        return false;
    }
    if (::Eden::DevVulkan::hcr_mode == 2) {
        return HostConditionalRenderingOnCpu(*this, *impl, object_1.address, 0);
    }
    if (::Eden::DevVulkan::hcr_mode == 1 && qc_dirty) {
        ::Eden::Performance::CountCondition(19);
        EndHostConditionalRendering();
        return false;
    }
    ::Eden::Performance::CountCondition(16);
    HostConditionalRenderingCompareBCImpl(object_1.address, true, true);
    return true;
}'''),
    ('''    if (!impl->device.IsExtConditionalRendering()) {
        return false;
    }

    const auto check_in_bc = [&](DAddr address) {''',
     '''    if (!impl->device.IsExtConditionalRendering()) {
        return false;
    }
    if (::Eden::DevVulkan::hcr_mode == 2) {
        return HostConditionalRenderingOnCpu(*this, *impl, object_1.address, equal_check ? 1 : 2);
    }

    const auto check_in_bc = [&](DAddr address) {'''),
    ('''    if (!is_in_ac[0] && !is_in_ac[1]) {
        EndHostConditionalRendering();
        return false;
    }

    if (!qc_dirty && !is_in_bc[0] && !is_in_bc[1]) {
        EndHostConditionalRendering();
        return false;
    }
''',
     '''    if (!is_in_ac[0] && !is_in_ac[1]) {
        ::Eden::Performance::CountCondition(13);
        EndHostConditionalRendering();
        return false;
    }

    if (!qc_dirty && !is_in_bc[0] && !is_in_bc[1]) {
        ::Eden::Performance::CountCondition(13);
        EndHostConditionalRendering();
        return false;
    }
    // hcr=exact: a pending host query is not in any buffer yet; the CPU evaluation waits for it.
    if (::Eden::DevVulkan::hcr_mode == 1 && qc_dirty) {
        ::Eden::Performance::CountCondition(19);
        EndHostConditionalRendering();
        return false;
    }
'''),
    ('''            HostConditionalRenderingCompareValueImpl(*objects[j], equal_check);
            return true;''',
     '''            ::Eden::Performance::CountCondition(15);
            HostConditionalRenderingCompareValueImpl(*objects[j], equal_check);
            return true;'''),
    ('''    if (!is_gpu_high) {
        EndHostConditionalRendering();
        return true;
    }

    if (!is_in_bc[0] && !is_in_bc[1]) {
        EndHostConditionalRendering();
        return true;
    }
    HostConditionalRenderingCompareBCImpl(object_1.address, equal_check);''',
     '''    // Eden draws these unconditionally; hcr=exact evaluates them on the CPU instead.
    if (!is_gpu_high || (!is_in_bc[0] && !is_in_bc[1])) {
        ::Eden::Performance::CountCondition(14);
        EndHostConditionalRendering();
        return ::Eden::DevVulkan::hcr_mode != 1;
    }
    ::Eden::Performance::CountCondition(16);
    HostConditionalRenderingCompareBCImpl(object_1.address, equal_check);'''),
])
adapt('src/video_core/renderer_vulkan/vk_swapchain.cpp', 'vulkan_swapchain.cpp', [
    # An acquisition error has no usable image index. Forward it to the existing
    # renderer recovery path instead of indexing or presenting an invalid image.
    ('LOG_ERROR(Render_Vulkan, "vkAcquireNextImageKHR returned {}", string_VkResult(result));',
     'LOG_ERROR(Render_Vulkan, "vkAcquireNextImageKHR returned {}", string_VkResult(result));\n        vk::Check(result);'),
    ('LOG_CRITICAL(Render_Vulkan, "Failed to present with error {}", string_VkResult(result));',
     'LOG_CRITICAL(Render_Vulkan, "Failed to present with error {}", string_VkResult(result));\n        vk::Check(result);'),
])
# Keep the overlay in the existing display composition pass (also used for
# frontend screenshots), excluding guest applet captures. Cache its pipeline.
# Preserve upstream ABI, including consumers whose quoted includes resolve to
# the original header. Display/capture purpose belongs to the draw call.
adapt('src/video_core/present.h', 'include/video_core/present.h', [])
adapt('src/video_core/renderer_vulkan/vk_present_manager.h',
      'include/video_core/renderer_vulkan/vk_present_manager.h', [
    ('#include <mutex>', '#include <mutex>\n#include <exception>'),
    ('    std::jthread present_thread;',
     '    std::exception_ptr present_failure;\n    std::jthread present_thread;'),
])
adapt('src/video_core/renderer_vulkan/vk_present_manager.cpp', 'vulkan_present_manager.cpp', [
    ('#include "video_core/renderer_vulkan/vk_present_manager.h"',
     '#include "video_core/renderer_vulkan/vk_present_manager.h"\n#include "gpu_failure.h"\n#include "performance.h"'),
    # GetRenderFrame runs on the GPU thread: time its free-frame and
    # present-completion waits (FIFO back-pressure on the producer).
    ('    // Wait for free presentation frames\n    std::unique_lock lock{free_mutex};',
     '    // Wait for free presentation frames\n    ::Eden::Performance::Timer present_wait_timer(::Eden::Performance::gpu_present_wait);\n    std::unique_lock lock{free_mutex};'),
    ('PresentThread(token);', '''try {
            PresentThread(token);
        } catch (...) {
            const auto error = std::current_exception();
            {
                std::scoped_lock lock{queue_mutex, free_mutex};
                present_failure = error;
                present_queue.clear();
            }
            free_cv.notify_all();
            frame_cv.notify_all();
            Eden::RecordGpuFailure(error);
        }'''),
    ('free_cv.wait(lock, [this] { return !free_queue.empty(); });',
     '''free_cv.wait(lock, [this] { return present_failure || !free_queue.empty(); });
    if (present_failure) std::rethrow_exception(present_failure);'''),
    ('            present_queue.push_back(frame);',
     '            if (present_failure) return;\n            present_queue.push_back(frame);'),
    ('frame_cv.wait(queue_lock, [this] { return present_queue.empty(); });',
     'frame_cv.wait(queue_lock, [this] { return present_failure || present_queue.empty(); });'),
])
# Descriptor buffers: Eden's null buffer (nullDescriptor devices) has device address 0,
# but WriteDescriptorBuffer passed it with the binding's range. RADV then builds a real
# descriptor at GPU address 0; desktop kernels only log the resulting page fault, the PS5
# kills the title (TCP read at VA 0).
# The spec requires a null pAddressInfo (or VK_WHOLE_SIZE) for a null descriptor.
# The allocator's budget is the driver's, and on the console it does not say what is available: the
# driver counts each allocation twice (as VRAM and as visible VRAM), so the budget of the 8 GiB heap
# is used up at about 4 GiB, and it knows nothing of the pool the CPU shares. Eden asks for images
# and buffers within the budget and takes a refusal as final. A refused image or buffer is asked for
# again outside the budget; the driver refuses when the pool has no room, which is still the end of
# the session, and stderr.log then says what the pool had.
ALLOCATOR_REFUSED = """    VkResult created = CALL;
    if (created != VK_SUCCESS) {
        VmaAllocationCreateInfo outside_budget = alloc_ci;
        outside_budget.flags &= ~static_cast<VmaAllocationCreateFlags>(VMA_ALLOCATION_CREATE_WITHIN_BUDGET_BIT);
        created = AGAIN;
        static std::atomic<unsigned long long> refused{0};
        const auto times = refused.fetch_add(1, std::memory_order_relaxed) + 1;
        if ((times & (times - 1)) == 0) { // the 1st, 2nd, 4th, 8th ... time
            std::fprintf(stderr, "EDEN_GRAPHICS_MEMORY budget refused %llu requests; asked again outside it: result %d, DETAIL\\n",
                         times, int(created), VALUES);
        }
        // Last: the guest's reserve, 256 MiB at a time (src/guest_memory_service.inc).
        for (int lent = 0; created != VK_SUCCESS && lent < 4; ++lent) {
            const std::size_t given = eden_guest_memory_lend(std::size_t{256} << 20);
            if (given == 0) break;
            created = AGAIN;
            std::fprintf(stderr, "EDEN_GRAPHICS_MEMORY lent %llu MiB of the guest's reserve: result %d\\n",
                         static_cast<unsigned long long>(given >> 20), int(created));
        }
        // Then the staging buffers the GPU is done with, which Eden lets go one size level a frame.
        if (created != VK_SUCCESS) {
            if (const unsigned long long released = ::Eden::Performance::ReleaseIdleStaging(); released != 0) {
                created = AGAIN;
                std::fprintf(stderr, "EDEN_GRAPHICS_MEMORY asked again after %llu MiB of idle staging buffers went: result %d\\n",
                             released >> 20, int(created));
            }
        }
        // Then the heap's empty spaces (the CPU's share of the pool), once.
        if (created != VK_SUCCESS && ::Eden::Performance::TrimHeap("a graphics buffer was refused") != 0) {
            created = AGAIN;
            std::fprintf(stderr, "EDEN_GRAPHICS_MEMORY asked again after the heap gave memory back: result %d\\n",
                         int(created));
        }
        if (created != VK_SUCCESS) {
            VmaBudget held[VK_MAX_MEMORY_HEAPS]{};
            vmaGetHeapBudgets(allocator, held);
            unsigned long long blocks = 0, used = 0;
            for (const VmaBudget& heap : held) {
                blocks += heap.statistics.blockBytes;
                used += heap.statistics.allocationBytes;
            }
            const auto free_block = ::Eden::Performance::graphics_memory_free.load(std::memory_order_relaxed);
            std::size_t guest_span = 0, guest_held = 0;
            ::Common::GuestMemoryUsage(&guest_span, &guest_held);
            std::fprintf(stderr, "EDEN_GRAPHICS_MEMORY refused outside the budget too (result %d): the pool's largest free "
                         "block is %llu MiB; the allocator holds %llu MiB in blocks, %llu MiB of it in use; the guest's "
                         "memory holds %llu MiB, the heap %llu MiB\\n",
                         int(created), free_block ? free_block() >> 20 : 0ull, blocks >> 20, used >> 20,
                         static_cast<unsigned long long>(guest_held >> 20),
                         static_cast<unsigned long long>(eden_heap_committed() >> 20));
            ::Eden::Performance::ReportHeap("a buffer was refused");
            ::Eden::Performance::ReportMappings("a buffer was refused", true);
            DumpAllocatorOnce(allocator);
        }
    }
    vk::Check(created);
"""
def refused(call, detail, values):
    return (ALLOCATOR_REFUSED.replace('CALL', call).replace('AGAIN', call.replace('&alloc_ci', '&outside_budget'))
            .replace('DETAIL', detail).replace('VALUES', values))
IMAGE_VALUES = 'ci.extent.width, ci.extent.height, ci.extent.depth, ci.arrayLayers, ci.mipLevels, unsigned(ci.samples), int(ci.format)'
BUFFER_VALUES = 'static_cast<unsigned long long>(ci.size), unsigned(ci.usage)'
adapt('src/video_core/vulkan_common/vulkan_memory_allocator.cpp', 'vulkan_memory_allocator.cpp', [
    # Images too: past the budget and the relaxed request, the guest's reserve, 256 MiB at a time
    # (src/guest_memory_service.inc), before an image with nothing bound. A Switch 2 game ended on
    # an ASTC transcode target with the whole 1 GiB reserve unused.
    ('        result = vmaCreateImage(allocator, &ci, &relaxed, &handle, &allocation, &alloc_info);\n',
     '        result = vmaCreateImage(allocator, &ci, &relaxed, &handle, &allocation, &alloc_info);\n'
     '        for (int lent = 0; result == VK_ERROR_OUT_OF_DEVICE_MEMORY && lent < 4; ++lent) {\n'
     '            const std::size_t given = eden_guest_memory_lend(std::size_t{256} << 20);\n'
     '            if (given == 0) break;\n'
     '            result = vmaCreateImage(allocator, &ci, &relaxed, &handle, &allocation, &alloc_info);\n'
     '            std::fprintf(stderr, "EDEN_GRAPHICS_MEMORY lent %llu MiB of the guest\'s reserve for an image: result %d\\n",\n'
     '                         static_cast<unsigned long long>(given >> 20), int(result));\n'
     '        }\n'
     '        if (result == VK_ERROR_OUT_OF_DEVICE_MEMORY) {\n'
     '            if (const unsigned long long released = ::Eden::Performance::ReleaseIdleStaging(); released != 0) {\n'
     '                result = vmaCreateImage(allocator, &ci, &relaxed, &handle, &allocation, &alloc_info);\n'
     '                std::fprintf(stderr, "EDEN_GRAPHICS_MEMORY asked again for an image after %llu MiB of idle staging buffers went: result %d\\n",\n'
     '                             released >> 20, int(result));\n'
     '            }\n'
     '        }\n'
     '        if (result == VK_ERROR_OUT_OF_DEVICE_MEMORY && ::Eden::Performance::TrimHeap("a graphics image was refused") != 0) {\n'
     '            result = vmaCreateImage(allocator, &ci, &relaxed, &handle, &allocation, &alloc_info);\n'
     '            std::fprintf(stderr, "EDEN_GRAPHICS_MEMORY asked again for an image after the heap gave memory back: result %d\\n", int(result));\n'
     '        }\n'
     '        if (result == VK_ERROR_OUT_OF_DEVICE_MEMORY) {\n'
     '            ::Eden::Performance::ReportMappings("an image was refused", true);\n'
     '            DumpAllocatorOnce(allocator);\n'
     '        }\n'),
    ('#include "video_core/vulkan_common/vulkan_memory_allocator.h"\n',
     '#include "video_core/vulkan_common/vulkan_memory_allocator.h"\n#include <atomic>\n#include <cstddef>\n#include <cstdio>\n#include "performance.h"\nnamespace Common { void GuestMemoryUsage(std::size_t* span, std::size_t* held) noexcept; }\nextern "C" std::size_t eden_heap_committed(void);\nextern "C" std::size_t eden_guest_memory_lend(std::size_t bytes) noexcept;\n'
     '// Every allocation the allocator holds (its detailed statistics), once: logs/vma-refused.json.\n'
     'namespace { void DumpAllocatorOnce(VmaAllocator allocator) {\n'
     '    static std::atomic<bool> done{false};\n'
     '    if (done.exchange(true)) return;\n'
     '    char* text = nullptr;\n'
     '    vmaBuildStatsString(allocator, &text, VK_TRUE);\n'
     '    if (text != nullptr) { ::Eden::Performance::WriteLogFile("vma-refused.json", text); vmaFreeStatsString(allocator, text); }\n'
     '} }\n'),
    # What an image was that the allocator could not make at all, for stderr.log.
    # (Lichii #440 returns the failure instead of throwing: the texture cache then leaves the
    # image out and makes it again after collecting.)
    ('    if (result != VK_SUCCESS) {\n        failed_images.fetch_add(1, std::memory_order_relaxed);',
     """    if (result != VK_SUCCESS) {
        failed_images.fetch_add(1, std::memory_order_relaxed);
        std::fprintf(stderr, "EDEN_IMAGE_REFUSED %ux%ux%u, %u layers, %u levels, format %d, type %d, usage 0x%x, flags 0x%x, "
                     "tiling %d, samples %d, result %d, huge %d, texels %llu\\n",
                     ci.extent.width, ci.extent.height, ci.extent.depth, ci.arrayLayers, ci.mipLevels, int(ci.format),
                     int(ci.imageType), unsigned(ci.usage), unsigned(ci.flags), int(ci.tiling), int(ci.samples),
                     int(result), int(huge), static_cast<unsigned long long>(texels));"""),
    # An image with nothing bound (the sparse virtual texture arrays no pool could hold): when the driver refuses
    # it with residency, it is asked for again with binding alone, and what it said is written down.
    ('    if (device.GetDispatchLoader().vkCreateImage(*device.GetLogical(), &sparse_ci, nullptr,\n'
     '                                                 &sparse) != VK_SUCCESS) {\n'
     '        return vk::Image{};\n'
     '    }\n',
     """    VkResult sparse_result = device.GetDispatchLoader().vkCreateImage(*device.GetLogical(), &sparse_ci, nullptr, &sparse);
    if (sparse_result != VK_SUCCESS) {
        VkImageCreateInfo binding_only = ci;
        binding_only.flags |= VK_IMAGE_CREATE_SPARSE_BINDING_BIT;
        const VkResult second = device.GetDispatchLoader().vkCreateImage(*device.GetLogical(), &binding_only, nullptr, &sparse);
        std::fprintf(stderr, "EDEN_SPARSE_IMAGE %ux%ux%u, %u layers, %u levels, format %d, usage 0x%x, flags 0x%x, tiling %d: "
                     "with residency %d, binding alone %d\\n",
                     ci.extent.width, ci.extent.height, ci.extent.depth, ci.arrayLayers, ci.mipLevels, int(ci.format),
                     unsigned(ci.usage), unsigned(ci.flags), int(ci.tiling), int(sparse_result), int(second));
        if (second != VK_SUCCESS) {
            // Once: which smaller sizes does the driver accept? Each probe is made and destroyed at once.
            static std::atomic<bool> probed{false};
            if (!probed.exchange(true)) {
                for (u32 layers = ci.arrayLayers / 2; layers >= 1; layers /= 2) {
                    VkImageCreateInfo probe = sparse_ci;
                    probe.arrayLayers = layers;
                    VkImage probe_image{};
                    const VkResult probed_result =
                        device.GetDispatchLoader().vkCreateImage(*device.GetLogical(), &probe, nullptr, &probe_image);
                    std::fprintf(stderr, "EDEN_SPARSE_PROBE %u layers: %d\\n", layers, int(probed_result));
                    if (probed_result == VK_SUCCESS) {
                        device.GetDispatchLoader().vkDestroyImage(*device.GetLogical(), probe_image, nullptr);
                    }
                    if (layers == 1) break;
                }
            }
            return vk::Image{};
        }
    }
"""),
    ('    vk::Check(vmaCreateBuffer(allocator, &ci, &alloc_ci, &handle, &allocation, &alloc_info));\n',
     refused('vmaCreateBuffer(allocator, &ci, &alloc_ci, &handle, &allocation, &alloc_info)',
             'buffer of %llu bytes, usage 0x%x', BUFFER_VALUES)),
    ('    vk::Check(vmaCreateBufferWithAlignment(allocator, &ci, &alloc_ci, min_alignment, &handle,\n'
     '                                           &allocation, &alloc_info));\n',
     refused('vmaCreateBufferWithAlignment(allocator, &ci, &alloc_ci, min_alignment, &handle,\n'
             '                                           &allocation, &alloc_info)',
             'buffer of %llu bytes, usage 0x%x', BUFFER_VALUES)),
])
# Staging buffers (headless/staging_buffers.h): above 16 MiB a buffer the CPU writes or reads is its
# transfer rounded up to 16 MiB instead of the next power of two, a kept buffer is handed out again
# only when it holds the transfer, and a refused buffer is asked for once more after the kept ones
# the GPU is done with are let go. A Switch 2 title was refused 512 MiB for a 341 MiB transfer with
# 480 MiB free. No Finish() when releasing: a buffer handed out earlier may not have its copy recorded.
adapt('src/video_core/renderer_vulkan/vk_staging_buffer_pool.cpp', 'vulkan_staging_buffer_pool.cpp', [
    ('#include "video_core/vulkan_common/vulkan_wrapper.h"\n',
     '#include "video_core/vulkan_common/vulkan_wrapper.h"\n#include <atomic>\n#include <cstdio>\n#include <thread>\n'
     '#include "performance.h"\n#include "staging_buffers.h"\n'),
    # A refused image or buffer lets the idle kept buffers go before the session ends on it
    # (performance.h ReleaseIdleStaging, after Nightshade 07105769). The pool's lists are its own
    # thread's: the frame tick, which runs there, records that thread, and a release asked from
    # another thread does nothing.
    ('StagingBufferPool::~StagingBufferPool() = default;\n',
     'namespace {\n'
     'std::atomic<StagingBufferPool*> staging_pool_alive{nullptr};\n'
     'std::atomic<std::thread::id> staging_pool_thread{};\n'
     'std::atomic<bool> staging_release_asked{false};\n'
     'std::atomic<unsigned long long> staging_released_bytes{0};\n'
     'unsigned long long ReleaseIdleStagingBuffers() {\n'
     '    StagingBufferPool* const pool = staging_pool_alive.load(std::memory_order_acquire);\n'
     '    if (pool == nullptr || staging_pool_thread.load(std::memory_order_relaxed) != std::this_thread::get_id()) {\n'
     '        return 0;\n'
     '    }\n'
     '    const unsigned long long before = staging_released_bytes.load(std::memory_order_relaxed);\n'
     '    staging_release_asked.store(true, std::memory_order_relaxed);\n'
     '    pool->TickFrame();\n'
     '    return staging_released_bytes.load(std::memory_order_relaxed) - before;\n'
     '}\n'
     '} // namespace\n\n'
     'StagingBufferPool::~StagingBufferPool() {\n'
     '    StagingBufferPool* self = this;\n'
     '    if (staging_pool_alive.compare_exchange_strong(self, nullptr, std::memory_order_acq_rel)) {\n'
     '        ::Eden::Performance::graphics_staging_release.store(nullptr, std::memory_order_release);\n'
     '    }\n'
     '}\n'),
    ('void StagingBufferPool::TickFrame() {\n'
     '    current_delete_level = (current_delete_level + 1) % NUM_LEVELS;\n',
     'void StagingBufferPool::TickFrame() {\n'
     '    staging_pool_thread.store(std::this_thread::get_id(), std::memory_order_relaxed);\n'
     '    if (staging_pool_alive.exchange(this, std::memory_order_acq_rel) != this) {\n'
     '        ::Eden::Performance::graphics_staging_release.store(&ReleaseIdleStagingBuffers, std::memory_order_release);\n'
     '    }\n'
     '    if (staging_release_asked.exchange(false, std::memory_order_relaxed)) {\n'
     '        const auto idle = [this](const StagingBuffer& entry) {\n'
     '            return !entry.deferred && scheduler.IsFree(entry.tick);\n'
     '        };\n'
     '        ::Eden::StagingBuffers::Released released;\n'
     '        for (StagingBuffersCache* const cache : {&device_local_cache, &upload_cache, &download_cache}) {\n'
     '            for (StagingBuffers& level : *cache) {\n'
     '                released += ::Eden::StagingBuffers::ReleaseFrom(level, idle);\n'
     '            }\n'
     '        }\n'
     '        staging_released_bytes.fetch_add(released.bytes, std::memory_order_relaxed);\n'
     '        return;\n'
     '    }\n'
     '    // At most kIdleCap of idle buffers kept, none while memory is short (staging_buffers.h).\n'
     '    {\n'
     '        const auto idle = [this](const StagingBuffer& entry) {\n'
     '            return !entry.deferred && scheduler.IsFree(entry.tick);\n'
     '        };\n'
     '        StagingBuffersCache* const caches[]{&device_local_cache, &upload_cache, &download_cache};\n'
     '        const bool short_memory = ::Eden::Performance::graphics_memory_short.load(std::memory_order_relaxed);\n'
     '        const ::Eden::StagingBuffers::Released capped = ::Eden::StagingBuffers::CapIdle(\n'
     '            caches, idle, short_memory ? 0 : ::Eden::StagingBuffers::kIdleCap);\n'
     '        staging_released_bytes.fetch_add(capped.bytes, std::memory_order_relaxed);\n'
     '    }\n'
     '    current_delete_level = (current_delete_level + 1) % NUM_LEVELS;\n'),
    ('    const auto is_free = [this](const StagingBuffer& entry) {\n'
     '        return !entry.deferred && scheduler.IsFree(entry.tick);\n'
     '    };',
     '    const auto is_free = [this, size](const StagingBuffer& entry) {\n'
     '        return !entry.deferred && scheduler.IsFree(entry.tick) &&\n'
     '               ::Eden::StagingBuffers::Holds(entry.mapped_span.size(), entry.log2_level) >= size;\n'
     '    };'),
    ('    auto const log2_size = Common::Log2Ceil<u32>(u32(size));\n',
     '    auto const log2_size = Common::Log2Ceil<u32>(u32(size));\n'
     '    const bool exact = ::Eden::StagingBuffers::Exact(size, usage != MemoryUsage::DeviceLocal);\n'
     '    const VkDeviceSize made = ::Eden::StagingBuffers::Size(size, log2_size, exact);\n'
     '    if (exact) {\n'
     '        // Kept buffers of this power of two too small for the transfer go before it is made.\n'
     '        ::Eden::StagingBuffers::ReleaseFrom(GetCache(usage)[log2_size], [this, size](const StagingBuffer& entry) {\n'
     '            return !entry.deferred && scheduler.IsFree(entry.tick) &&\n'
     '                   ::Eden::StagingBuffers::Holds(entry.mapped_span.size(), entry.log2_level) < size;\n'
     '        });\n'
     '    }\n'),
    ('        .size = 1ULL << log2_size,\n', '        .size = made,\n'),
    ('    vk::Buffer buffer = memory_allocator.CreateBuffer(buffer_ci, usage);\n',
     '    vk::Buffer buffer;\n'
     '    try {\n'
     '        buffer = memory_allocator.CreateBuffer(buffer_ci, usage);\n'
     '    } catch (const vk::Exception& refused) {\n'
     '        if (refused.GetResult() != VK_ERROR_OUT_OF_DEVICE_MEMORY) {\n'
     '            throw;\n'
     '        }\n'
     '        // Refused: the kept buffers the GPU is done with go, and the buffer is asked for once\n'
     '        // more. Refused again, the error goes on as before.\n'
     '        const auto idle = [this](const StagingBuffer& entry) {\n'
     '            return !entry.deferred && scheduler.IsFree(entry.tick);\n'
     '        };\n'
     '        ::Eden::StagingBuffers::Released released;\n'
     '        for (StagingBuffersCache* const cache : {&device_local_cache, &upload_cache, &download_cache}) {\n'
     '            for (StagingBuffers& level : *cache) {\n'
     '                released += ::Eden::StagingBuffers::ReleaseFrom(level, idle);\n'
     '            }\n'
     '        }\n'
     '        std::fprintf(stderr, "EDEN_STAGING_OOM asked=%zu made=%llu released=%zu buffers (%llu MiB)\\n", size,\n'
     '                     static_cast<unsigned long long>(made), released.buffers,\n'
     '                     static_cast<unsigned long long>(released.bytes >> 20));\n'
     '        buffer = memory_allocator.CreateBuffer(buffer_ci, usage);\n'
     '    }\n'
     '    // A large buffer smaller than the power of two: the 1st, 2nd, 4th, 8th ... in the log.\n'
     '    if (exact && made >= (VkDeviceSize{64} << 20) && made < (VkDeviceSize{1} << log2_size)) {\n'
     '        static std::atomic<unsigned long long> large{0};\n'
     '        const auto times = large.fetch_add(1, std::memory_order_relaxed) + 1;\n'
     '        if ((times & (times - 1)) == 0) {\n'
     '            std::fprintf(stderr, "EDEN_STAGING_EXACT %llu MiB made for a transfer of %llu MiB, not the %llu MiB of the "\n'
     '                         "next power of two (%llu so far)\\n",\n'
     '                         static_cast<unsigned long long>(made >> 20), static_cast<unsigned long long>(size >> 20),\n'
     '                         static_cast<unsigned long long>((VkDeviceSize{1} << log2_size) >> 20), times);\n'
     '        }\n'
     '    }\n'),
])
# Development A/B switches for device features (headless/dev_vulkan.h, dev-settings).
adapt('src/video_core/vulkan_common/vulkan_device.cpp', 'vulkan_device.cpp', [
    ('#include <algorithm>', '#include <algorithm>\n#include <cstdio>\n#include "dev_vulkan.h"\n#include "performance.h"'),
    # "Memory in use" for the texture and buffer caches: on the console, the budget less what is
    # left of the pool the CPU shares with the GPU (performance.h, graphics_memory_free).
    ('u64 Device::GetDeviceMemoryUsage() const {\n',
     'namespace {\n'
     '// The largest block of the shared pool that was free when the device was made: about the most\n'
     '// graphics can be given in this session.\n'
     'u64 graphics_capacity = 0;\n'
     '}\n\n'
     'u64 Device::GetDeviceMemoryUsage() const {\n'
     '    if (const auto free_memory = ::Eden::Performance::graphics_memory_free.load(std::memory_order_relaxed);\n'
     '        free_memory && ::Eden::Performance::graphics_usage_from_pool.load(std::memory_order_relaxed)) {\n'
     '        // The caches evict what is unused 1.6 GiB under the budget and evict hard 0.8 GiB under\n'
     '        // it. Where the pool could never give graphics 6.4 GiB, those marks become a quarter\n'
     '        // and an eighth of what it could give.\n'
     '        const double scale = graphics_capacity != 0 && graphics_capacity < 6400_MiB\n'
     '                                 ? static_cast<double>(6400_MiB) / static_cast<double>(graphics_capacity)\n'
     '                                 : 1.0;\n'
     '        const u64 free_bytes = static_cast<u64>(static_cast<double>(free_memory()) * scale);\n'
     '        return device_access_memory > free_bytes ? device_access_memory - free_bytes : 0;\n'
     '    }\n'),
    ('    extensions.descriptor_buffer = features.descriptor_buffer.descriptorBuffer;',
     '    extensions.descriptor_buffer = features.descriptor_buffer.descriptorBuffer &&\n'
     '        !::Eden::DevVulkan::disable_descriptor_buffer;'),
    ('    features.robustness2.robustBufferAccess2 = VK_FALSE;\n'
     '    features.robustness2.robustImageAccess2 = VK_FALSE;\n'
     '    extensions.robustness_2 = features.robustness2.nullDescriptor;',
     '    if (!::Eden::DevVulkan::robustness2) {\n'
     '        features.robustness2.robustBufferAccess2 = VK_FALSE;\n'
     '        features.robustness2.robustImageAccess2 = VK_FALSE;\n'
     '    }\n'
     '    if (::Eden::DevVulkan::disable_null_descriptor) {\n'
     '        features.robustness2.nullDescriptor = VK_FALSE;\n'
     '    }\n'
     '    extensions.robustness_2 = features.robustness2.nullDescriptor;'),
    ('            (queue_family_properties[*graphics].queueFlags & VK_QUEUE_SPARSE_BINDING_BIT) != 0;',
     '            (queue_family_properties[*graphics].queueFlags & VK_QUEUE_SPARSE_BINDING_BIT) != 0 &&\n'
     '            !::Eden::DevVulkan::disable_sparse;'),
    ('            features.custom_border_color.customBorderColorWithoutFormat;\n    }\n'
     '    RemoveExtensionFeatureIfUnsuitable(extensions.custom_border_color',
     '            features.custom_border_color.customBorderColorWithoutFormat &&\n'
     '            !::Eden::DevVulkan::disable_custom_border;\n    }\n'
     '    RemoveExtensionFeatureIfUnsuitable(extensions.custom_border_color'),
    # dev-settings conditional_rendering=off: guest render-enable conditions are resolved
    # without VK_EXT_conditional_rendering (the path Eden takes on drivers without it).
    ('    // VK_EXT_border_color_swizzle\n',
     '    if (::Eden::DevVulkan::disable_conditional_rendering) {\n'
     '        extensions.conditional_rendering = false;\n    }\n'
     '    // VK_EXT_border_color_swizzle\n'),
    # The heaps and the budget the texture/buffer caches size themselves from.
    ('            device_access_memory = std::min<u64>(device_access_memory, normal_memory + scaler_memory);\n'
     '        }\n    }\n}\n',
     '            device_access_memory = std::min<u64>(device_access_memory, normal_memory + scaler_memory);\n'
     '        }\n    }\n'
     '    if (const auto free_memory = ::Eden::Performance::graphics_memory_free.load(std::memory_order_relaxed)) {\n'
     '        graphics_capacity = free_memory();\n'
     '    }\n'
     '    std::printf("EDEN_VULKAN_MEMORY integrated=%d heaps=%zu local=%llu initial_usage=%llu device_access=%llu "\n'
     '                "capacity=%llu\\n",\n'
     '                int(is_integrated), valid_heap_memory.size(), static_cast<unsigned long long>(local_memory),\n'
     '                static_cast<unsigned long long>(device_initial_usage),\n'
     '                static_cast<unsigned long long>(device_access_memory),\n'
     '                static_cast<unsigned long long>(graphics_capacity));\n}\n'),
])
# Development GPU fault probe (dev_vulkan.h, submit_sync=on): wait for every
# submission, so the CPU never records against work the GPU has not finished.
GPU_TIME_HELPERS = r'''
namespace {
// Development GPU execution time (dev_vulkan.h gpu_time): timestamps at the start and end
// of every scheduler command buffer, summed once the GPU has run them and printed every
// 5 s as EDEN_GPU_TIME. Worker thread only.
struct GpuTimeProbe {
    static constexpr u32 SLOTS = 512;
    vk::QueryPool pool;
    PFN_vkCmdWriteTimestamp write{};
    double period_ns{};
    u64 next{}, first_pending{};
    u32 current{};
    u64 busy_ns{}, max_ns{}, submissions{};
    std::chrono::steady_clock::time_point report{};
    bool failed{};
} gpu_time;

void GpuTimeBegin(const Device& device, vk::CommandBuffer cmdbuf) {
    if (!::Eden::DevVulkan::gpu_time || gpu_time.failed) return;
    if (!gpu_time.write) {
        const auto& dld = device.GetDispatchLoader();
        gpu_time.write = reinterpret_cast<PFN_vkCmdWriteTimestamp>(
            dld.vkGetDeviceProcAddr(*device.GetLogical(), "vkCmdWriteTimestamp"));
        gpu_time.period_ns = device.GetPhysical().GetProperties().limits.timestampPeriod;
        if (!gpu_time.write || gpu_time.period_ns <= 0) { gpu_time.failed = true; return; }
        gpu_time.pool = device.GetLogical().CreateQueryPool({
            .sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO,
            .pNext = nullptr,
            .flags = 0,
            .queryType = VK_QUERY_TYPE_TIMESTAMP,
            .queryCount = GpuTimeProbe::SLOTS * 2,
            .pipelineStatistics = 0,
        });
        gpu_time.report = std::chrono::steady_clock::now();
    }
    if (gpu_time.next - gpu_time.first_pending >= GpuTimeProbe::SLOTS) {
        gpu_time.failed = true; // results stopped arriving; never overwrite a pending slot
        std::printf("EDEN_GPU_TIME failed=ring\n");
        return;
    }
    gpu_time.current = static_cast<u32>(gpu_time.next++ % GpuTimeProbe::SLOTS);
    cmdbuf.ResetQueryPool(*gpu_time.pool, gpu_time.current * 2, 2);
    gpu_time.write(*cmdbuf, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, *gpu_time.pool, gpu_time.current * 2);
}

void GpuTimeEnd(vk::CommandBuffer cmdbuf) {
    if (!::Eden::DevVulkan::gpu_time || gpu_time.failed || !gpu_time.write) return;
    gpu_time.write(*cmdbuf, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, *gpu_time.pool, gpu_time.current * 2 + 1);
}

void GpuTimeCollect(const Device& device) {
    if (!::Eden::DevVulkan::gpu_time || gpu_time.failed || !gpu_time.write) return;
    while (gpu_time.first_pending + 1 < gpu_time.next) {
        const u32 slot = static_cast<u32>(gpu_time.first_pending % GpuTimeProbe::SLOTS);
        std::array<u64, 2> stamps{};
        if (device.GetLogical().GetQueryResults(*gpu_time.pool, slot * 2, 2, sizeof(stamps), stamps.data(),
                                               sizeof(u64), VK_QUERY_RESULT_64_BIT) != VK_SUCCESS) break;
        const u64 ns = stamps[1] > stamps[0]
                           ? static_cast<u64>(double(stamps[1] - stamps[0]) * gpu_time.period_ns) : 0;
        gpu_time.busy_ns += ns;
        gpu_time.max_ns = (std::max)(gpu_time.max_ns, ns);
        ++gpu_time.submissions;
        ++gpu_time.first_pending;
    }
    const auto now = std::chrono::steady_clock::now();
    const auto wall = std::chrono::duration_cast<std::chrono::nanoseconds>(now - gpu_time.report).count();
    if (wall >= 5'000'000'000) {
        std::printf("EDEN_GPU_TIME wall_ms=%.1f busy_ms=%.1f submissions=%llu max_ms=%.2f\n", wall / 1e6,
                    gpu_time.busy_ns / 1e6, static_cast<unsigned long long>(gpu_time.submissions),
                    gpu_time.max_ns / 1e6);
        gpu_time.busy_ns = gpu_time.max_ns = gpu_time.submissions = 0;
        gpu_time.report = now;
    }
}
} // namespace
'''
adapt('src/video_core/renderer_vulkan/vk_scheduler.cpp', 'vulkan_scheduler.cpp', [
    ('#include <memory>', '#include <memory>\n#include <array>\n#include <chrono>\n#include <cstdio>\n#include "dev_vulkan.h"'),
    ('namespace Vulkan {\n', 'namespace Vulkan {\n' + GPU_TIME_HELPERS),
    ('        .pInheritanceInfo = nullptr,\n    });\n    current_upload_cmdbuf = vk::CommandBuffer(command_pool->Commit(), device.GetDispatchLoader());',
     '        .pInheritanceInfo = nullptr,\n    });\n    GpuTimeBegin(device, current_cmdbuf);\n'
     '    current_upload_cmdbuf = vk::CommandBuffer(command_pool->Commit(), device.GetDispatchLoader());'),
    ('        cmdbuf.End();\n\n        if (on_submit) {',
     '        GpuTimeEnd(cmdbuf);\n        cmdbuf.End();\n\n        if (on_submit) {'),
    ('            trace_frame_end = 0;\n        }\n    });\n    chunk->MarkSubmit();',
     '            trace_frame_end = 0;\n        }\n        GpuTimeCollect(device);\n    });\n    chunk->MarkSubmit();'),
    ('    const u64 signal_value = SubmitExecution(signal_semaphore, wait_semaphore);\n'
     '    AllocateNewContext();\n    last_flush = std::chrono::steady_clock::now();',
     '    const u64 signal_value = SubmitExecution(signal_semaphore, wait_semaphore);\n'
     '    if (::Eden::DevVulkan::sync_submissions) {\n        Wait(signal_value);\n    }\n'
     '    AllocateNewContext();\n    last_flush = std::chrono::steady_clock::now();'),
])
adapt('src/video_core/renderer_vulkan/pipeline_helper.h',
      'include/video_core/renderer_vulkan/pipeline_helper.h', [
    ('            VkDescriptorGetInfoEXT get_info{',
     '            const VkDescriptorAddressInfoEXT* const buffer_info{\n'
     '                entry.address.address != 0 ? &address_info : nullptr};\n'
     '            VkDescriptorGetInfoEXT get_info{'),
    ('get_info.data.pUniformBuffer = &address_info;', 'get_info.data.pUniformBuffer = buffer_info;'),
    ('get_info.data.pStorageBuffer = &address_info;', 'get_info.data.pStorageBuffer = buffer_info;'),
    ('get_info.data.pUniformTexelBuffer = &address_info;', 'get_info.data.pUniformTexelBuffer = buffer_info;'),
    ('get_info.data.pStorageTexelBuffer = &address_info;', 'get_info.data.pStorageTexelBuffer = buffer_info;'),
])
adapt('src/video_core/renderer_vulkan/vk_blit_screen.h',
      'include/video_core/renderer_vulkan/vk_blit_screen.h', [
    ('VkFormat current_swapchain_view_format);',
     'VkFormat current_swapchain_view_format, bool draw_hud = false);'),
])
adapt('src/video_core/renderer_vulkan/renderer_vulkan.h',
      'include/video_core/renderer_vulkan/renderer_vulkan.h', [
    ('    void Composite(', '    void PresentLoading();\n    void Composite('),
])
adapt('src/video_core/renderer_vulkan/renderer_vulkan.cpp', 'vulkan_renderer.cpp', [
    ('#include "video_core/renderer_vulkan/renderer_vulkan.h"',
     '#include "video_core/renderer_vulkan/renderer_vulkan.h"\n#include "display_refresh.h"\n'
     '#include "diagnostics.h"\n#include "hud.h"'),
    # The HUD counted the guest rate only: OnFrameDisplayed() runs once per Composite(), while the
    # frames frame generation adds are presented through PresentManager::Present() (hud.h). Count
    # every frame handed to the present manager.
    ('        present_manager.Present(generated);\n    }',
     '        present_manager.Present(generated);\n'
     '        Eden::CountPresentedFrame();\n'
     '    }'),
    # A frame the display has no refresh for is not presented: a game patched for more frames
    # than the output shows keeps its pace instead of waiting for a refresh per frame
    # (headless/display_refresh.h). The frame still ends as any other.
    ('    RenderScreenshot(framebuffers);\n    const auto acquiring',
     '    RenderScreenshot(framebuffers);\n'
     '    if (::Eden::Display::SkipFrame()) {\n'
     '        gpu.RendererFrameEndNotify();\n'
     '        rasterizer.TickFrame();\n'
     '        return;\n'
     '    }\n'
     '    const auto acquiring'),
    ('    Report();', '    PresentLoading();\n    Report();'),
    ('swapchain.GetImageViewFormat());', 'swapchain.GetImageViewFormat(), true);'),
    ('layout, 1, format);', 'layout, 1, format, true);'),
    ('void RendererVulkan::Report() const {',
     (port / 'vulkan_loading.inc').read_text() + '\nvoid RendererVulkan::Report() const {'),
    # Present() only records the hand-off into a fresh chunk. Dispatch it now so
    # the present thread gets this frame immediately instead of after the
    # producer's next 8-draw dispatch (upstream does this only with LSFG).
    ('#ifdef HAS_LSFG\n    scheduler.DispatchWork();\n#endif\n\n    gpu.RendererFrameEndNotify();',
     '    Eden::CountPresentedFrame();\n    scheduler.DispatchWork();\n\n    gpu.RendererFrameEndNotify();'),
    ('} // namespace Vulkan', '''} // namespace Vulkan
namespace Eden {
void PresentVulkanLoading(VideoCore::RendererBase& renderer) {
    static_cast<Vulkan::RendererVulkan&>(renderer).PresentLoading();
}
}'''),
])
adapt('src/video_core/renderer_vulkan/present/window_adapt_pass.h',
      'include/video_core/renderer_vulkan/present/window_adapt_pass.h', [
    ('const Layout::FramebufferLayout& layout, Frame* dst);',
     'const Layout::FramebufferLayout& layout, Frame* dst, bool draw_hud = false);'),
    ('    vk::Pipeline coverage_pipeline;',
     '    vk::Pipeline coverage_pipeline;\n    vk::PipelineLayout hud_layout;\n    vk::Pipeline hud_pipeline;'),
])
adapt('src/video_core/renderer_vulkan/present/window_adapt_pass.cpp', 'vulkan_window_adapt_pass.cpp', [
    ('#include "core/frontend/framebuffer_layout.h"',
     '#include "core/frontend/framebuffer_layout.h"\n#include "hud.h"\n#include "vulkan_hud_shaders.h"'),
    ('const Layout::FramebufferLayout& layout, Frame* dst) {',
     'const Layout::FramebufferLayout& layout, Frame* dst, bool draw_hud) {'),
    ('    scheduler.Record([=](vk::CommandBuffer cmdbuf) {',
     (port / 'vulkan_hud_prepare.inc').read_text() + '\n    scheduler.Record([=](vk::CommandBuffer cmdbuf) {'),
    ('        cmdbuf.EndRenderPass();',
     (port / 'vulkan_hud_draw.inc').read_text() + '\n        cmdbuf.EndRenderPass();'),
])
adapt('src/video_core/renderer_vulkan/vk_blit_screen.cpp', 'vulkan_blit_screen.cpp', [
    ('VkFormat current_swapchain_view_format) {',
     'VkFormat current_swapchain_view_format, bool draw_hud) {'),
    ('image_index, layers, framebuffers, layout, frame);',
     'image_index, layers, framebuffers, layout, frame, draw_hud);'),
])
shader_header = '#pragma once\n#include <cstdint>\n'
for stage in ('vert', 'frag'):
    binary = output / f'vulkan_hud.{stage}.spv'
    subprocess.run(['glslangValidator', '-V', '--target-env', 'vulkan1.0',
                    '-o', str(binary), str(port / f'vulkan_hud.{stage}')], check=True)
    subprocess.run(['spirv-val', '--target-env', 'vulkan1.0', str(binary)], check=True)
    data = binary.read_bytes()
    words = struct.unpack('<' + 'I' * (len(data) // 4), data)
    shader_header += f'inline constexpr uint32_t EDEN_HUD_{stage.upper()}_SPV[] = {{\n'
    shader_header += ','.join(hex(word) for word in words) + '\n};\n'
path = output / 'vulkan_hud_shaders.h'
if not path.exists() or path.read_text() != shader_header:
    path.write_text(shader_header)
adapt('src/video_core/vulkan_common/vulkan.h', 'include/video_core/vulkan_common/vulkan.h',
      [('#else\n#define VK_USE_PLATFORM_XLIB_KHR', '#elif defined(PS5_NATIVE)\n// Native display-plane presentation, no desktop window-system headers.\n#else\n#define VK_USE_PLATFORM_XLIB_KHR')])
adapt('src/video_core/vulkan_common/vulkan_instance.cpp', 'vulkan_instance.cpp', [
    ('namespace Vulkan {', 'extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance, const char*);\n\nnamespace Vulkan {'),
    ('    case Core::Frontend::WindowSystemType::Headless:', '    case Core::Frontend::WindowSystemType::PS5:\n        extensions.push_back(VK_KHR_DISPLAY_EXTENSION_NAME);\n        break;\n    case Core::Frontend::WindowSystemType::Headless:'),
    ('#else\n    case Core::Frontend::WindowSystemType::X11:', '#elif !defined(PS5_NATIVE)\n    case Core::Frontend::WindowSystemType::X11:'),
    ('''    if (!library.IsOpen()) {
        LOG_ERROR(Render_Vulkan, "Vulkan library not available");
        throw vk::Exception(VK_ERROR_INITIALIZATION_FAILED);
    }
    if (!library.GetSymbol("vkGetInstanceProcAddr", &dld.vkGetInstanceProcAddr)) {
        LOG_ERROR(Render_Vulkan, "vkGetInstanceProcAddr not present in Vulkan");
        throw vk::Exception(VK_ERROR_INITIALIZATION_FAILED);
    }''', '    (void)library;\n    dld.vkGetInstanceProcAddr = &::vkGetInstanceProcAddr;'),
])
adapt('src/video_core/vulkan_common/vulkan_library.cpp', 'vulkan_library.cpp', [
    ('#if defined(__ANDROID__) && defined(ARCHITECTURE_arm64)', '#if defined(PS5_NATIVE)\n    return std::make_shared<Common::DynamicLibrary>();\n#elif defined(__ANDROID__) && defined(ARCHITECTURE_arm64)'),
])
adapt('src/video_core/vulkan_common/vulkan_wrapper.h', 'include/video_core/vulkan_common/vulkan_wrapper.h', [
    ('#include <array>', '#include <array>\n#include <cstdio>\n#include "performance.h"'),
    ('return dld->vkQueueSubmit(queue,', 'auto timer = Eden::Performance::VulkanTimer(2);\n        return dld->vkQueueSubmit(queue,'),
    ('return dld->vkQueueSubmit2(queue,', 'auto timer = Eden::Performance::VulkanTimer(2);\n        return dld->vkQueueSubmit2(queue,'),
    ('return dld->vkWaitForFences(owner,', 'auto timer = Eden::Performance::VulkanTimer(3);\n        return dld->vkWaitForFences(owner,'),
    ('const VkResult result = dld->vkWaitSemaphores(owner,', 'auto timer = Eden::Performance::VulkanTimer(4);\n        const VkResult result = dld->vkWaitSemaphores(owner,'),
    ('return dld->vkDeviceWaitIdle(handle);', 'auto timer = Eden::Performance::VulkanTimer(5);\n        return dld->vkDeviceWaitIdle(handle);'),
    ('''inline void Check(VkResult result) {
    if (result != VK_SUCCESS) {
        throw Exception(result);''', '''inline void Check(VkResult result, const char* file = __builtin_FILE(),
                  int line = __builtin_LINE(), const char* operation = __builtin_FUNCTION()) {
    if (result != VK_SUCCESS) {
        std::fprintf(stderr, "[EglantinePS5] Vulkan failure: %s:%d (%s) VkResult=%d\\n",
                     file, line, operation, static_cast<int>(result));
        throw Exception(result);'''),
])
adapt('src/video_core/vulkan_common/vulkan_wrapper.cpp', 'vulkan_wrapper.cpp', [
    ('#include "video_core/vulkan_common/vulkan_wrapper.h"', '#include "video_core/vulkan_common/vulkan_wrapper.h"\n#include "performance.h"'),
    ('auto const result = dld->vkCreateGraphicsPipelines(', 'auto timer = Eden::Performance::VulkanTimer(0);\n    auto const result = dld->vkCreateGraphicsPipelines('),
    ('Check(dld->vkCreateComputePipelines(', 'auto timer = Eden::Performance::VulkanTimer(1);\n    Check(dld->vkCreateComputePipelines('),
])
download_source = (source / 'src/video_core/texture_cache/texture_cache.h').read_text()
download_begin = download_source.index('void TextureCache<P>::DownloadMemory(DAddr cpu_addr, size_t size)')
download_loop = download_source.index('    for (const ImageId image_id : images) {', download_begin)
download_prefix = download_source[download_begin:download_loop]
# Reuse DeleteImage's exact accounting to stop lookahead when its existing
# pressure policy changes. Fail on source drift rather than inventing a budget.
delete_begin = download_source.index('    if (image.HasScaled()) {', download_source.index('void TextureCache<P>::DeleteImage('))
delete_end = download_source.index('    const GPUVAddr gpu_addr', delete_begin)
reclaimed_bytes = (download_source[delete_begin:delete_end]
                   .replace('total_used_memory -=', 'bytes +=')
                   .replace('slot_images[image_id]', 'image'))
assert 'image_id' not in reclaimed_bytes, 'DeleteImage accounting needs adapting to an image reference'
gc_begin = download_source.index('void TextureCache<P>::RunGarbageCollector(bool out_of_memory) {')
gc_end = download_source.index('\ntemplate <class P>', gc_begin)
gc_original = download_source[gc_begin:gc_end]
gc = gc_original
gc_setup = '''    const auto ReclaimedBytes = [&](const Image& image) {
        u64 bytes = 0;
''' + reclaimed_bytes + '''        return bytes;
    };
''' + (port / 'vulkan_gc_downloads.inc').read_text()
for old, new in [
    ('    const auto Cleanup = [this, &num_iterations, &high_priority_mode, &aggressive_mode,\n'
     '                          &evict_old_costly, out_of_memory]',
     gc_setup + '\n    const auto Cleanup = [&, this]'),
    ('        if (must_download) {', '        if (must_download && !TakePrefetched(image_id, image)) {'),
    # Time the per-image drain Cleanup still takes for an image the prefetch could not batch.
    # One GPU pipeline wait per eviction, against one wait for a whole batch in the batched path:
    # this is the stall the prefetch exists to remove, so keep it visible.
    ('            image.DownloadMemory(map, copies);\n            runtime.Finish();\n'
     '            SwizzleImage(*gpu_memory, image.gpu_addr, image.info, copies, map.mapped_span, swizzle_data_buffer);',
     '            image.DownloadMemory(map, copies);\n'
     '            {\n'
     '                const ::Eden::Performance::Timer drain_timer(\n'
     '                    ::Eden::Performance::gc_unbatched_drain, 1);\n'
     '                runtime.Finish();\n'
     '            }\n'
     '            SwizzleImage(*gpu_memory, image.gpu_addr, image.info, copies, map.mapped_span, swizzle_data_buffer);'),
    ('(!high_priority_mode && must_download)) {', '(!DirtyEvictions() && must_download)) {'),
    ('    Configure(false);', '    Configure(false);\n    PrefetchDownloads();'),
    ('        Configure(true);', '        Configure(true);\n        PrefetchDownloads();'),
]:
    if gc.count(old) != 1:
        raise RuntimeError(f'Pinned GC anchor changed: {old}')
    gc = gc.replace(old, new)
texture_costs = []
for signature, index in (
    ('RunGarbageCollector(bool out_of_memory)', 13), ('DownloadMemory(DAddr cpu_addr, size_t size)', 14),
    ('PopAsyncFlushes()', 15),
):
    anchor = f'void TextureCache<P>::{signature} {{'
    scope = f'''
    std::optional<Eden::Performance::Timer> cost_timer;
    if constexpr (std::is_same_v<Runtime, Vulkan::TextureCacheRuntime>) {{
        if (Eden::Performance::vulkan_cost_enabled.load(std::memory_order_relaxed))
            cost_timer.emplace(Eden::Performance::vulkan_api[{index}]);
    }}
'''
    if index == 13:
        scope += '''    if constexpr (std::is_same_v<Runtime, Vulkan::TextureCacheRuntime>) {
        if ((cost_timer || Eden::Performance::texture_budget_log.load(std::memory_order_relaxed)) &&
            frame_tick % 300 == 0)
            std::printf("EDEN_VULKAN_TEXTURE_BUDGET frame=%llu usage=%llu expected=%llu critical=%llu "
                        "keep_dirty=%d memory_short=%d\\n",
                        static_cast<unsigned long long>(frame_tick),
                        static_cast<unsigned long long>(total_used_memory),
                        static_cast<unsigned long long>(expected_memory),
                        static_cast<unsigned long long>(critical_memory),
                        int(Eden::Performance::gc_keep_dirty.load(std::memory_order_relaxed)),
                        int(Eden::Performance::graphics_memory_short.load(std::memory_order_relaxed)));
    }
'''
    texture_costs.append((anchor, anchor + scope))
adapt('src/video_core/texture_cache/texture_cache.h',
      'vulkan-cache/video_core/texture_cache/texture_cache.h', [
    ('namespace VideoCommon {', '#include "performance.h"\n#include "staging_chunks.h"\n#include "common/scope_exit.h"\nnamespace Vulkan { class TextureCacheRuntime; }\n\nnamespace VideoCommon {\n'
     '// True while UpdateRenderTargets looks up the render targets (JoinImages: stale guest memory).\n'
     'inline thread_local bool eden_preparing_render_targets = false;'),
    # A render target made again at a new size, over images the GPU drew into, is not uploaded from
    # guest memory: the GPU's drawing never reached guest memory, so what is there is stale. Ocarina
    # of Time resizes a few render targets nearly every frame (95, 96, 97, 112x97 ... up to 1736
    # pixels square at 0x814de35000) and each new size was uploaded, 0.5 to 5 GiB per 600 frames
    # (2026-11-07 23:20). Only when an image it overlaps that did not fit was drawn by the GPU and none
    # has CPU writes pending; the image is tracked so a later CPU write still uploads. For render
    # targets and for images read at a new size alike: Ocarina draws 1504x1491 and reads 1491x1491 at
    # the same address the next moment, and the read copy, uploaded from guest memory, holds nothing
    # the GPU drew either (23:36: 2,052 render targets skipped, the reads still 541-651 per 600 frames).
    # The GPU-drawn image must start at the same address and have the same format: the same target
    # at another size. Any GPU-drawn overlap was too wide: GPU-drawn images are not tracked, so CPU
    # writes into memory a render target had used (a texture loaded there) did not mark them, and
    # the new texture would not be uploaded.
    ('    } clearing_scope{clearing_render_targets};\n    clearing_render_targets = is_clear;\n',
     '    } clearing_scope{clearing_render_targets};\n    clearing_render_targets = is_clear;\n'
     '    eden_preparing_render_targets = true;\n'
     '    SCOPE_EXIT { eden_preparing_render_targets = false; };\n'),
    ('    } else {\n        // TODO: Only upload what we need\n        RefreshContents(new_image, new_image_id);\n    }\n',
     '    } else if (!join_bad_overlap_ids.empty() &&\n'
     '               std::ranges::any_of(join_bad_overlap_ids, [&](ImageId id) {\n'
     '                   const ImageBase& overlap = slot_images[id];\n'
     '                   return True(overlap.flags & ImageFlagBits::GpuModified) &&\n'
     '                          overlap.gpu_addr == new_image.gpu_addr && overlap.info.format == new_info.format;\n'
     '               }) &&\n'
     '               std::ranges::none_of(join_bad_overlap_ids, [&](ImageId id) {\n'
     '                   return True(slot_images[id].flags & ImageFlagBits::CpuModified);\n'
     '               })) {\n'
     '        static u64 eden_stale_skips = 0;\n'
     '        if (eden_stale_skips++ % 1024 < 4) {\n'
     '            LOG_INFO(HW_GPU, "EDEN_TC_STALE {} {:#x} {}x{} format {}: not uploaded over {} images, one GPU-drawn ({} so far)",\n'
     '                     eden_preparing_render_targets ? "render target" : "read", new_image.gpu_addr,\n'
     '                     new_info.size.width, new_info.size.height, static_cast<u32>(new_info.format),\n'
     '                     join_bad_overlap_ids.size(), eden_stale_skips);\n'
     '        }\n'
     '        new_image.flags &= ~ImageFlagBits::CpuModified;\n'
     '        if (False(new_image.flags & ImageFlagBits::Tracked)) {\n'
     '            TrackImage(new_image, new_image_id);\n'
     '        }\n'
     '        runtime.TransitionImageLayout(new_image);\n'
     '    } else {\n        // TODO: Only upload what we need\n        RefreshContents(new_image, new_image_id);\n    }\n'),
    (download_prefix, download_prefix + (port / 'vulkan_download_batch.inc').read_text() + '\n'),
    # Uploads UploadInChunks leaves whole (images converted on the CPU, 3D, sparse and unbacked
    # ones) are laid out in the heap and staged in pieces of at most 64 MiB once over 64 MiB
    # (headless/staging_chunks.h, after Nightshade 07105769): a converted texture array asked for
    # one staging buffer of its whole size, one run of the pool. GPU-decoded (accelerated)
    # uploads keep their own layer paths. Vulkan only: this file is compiled for OpenGL too.
    ('    if (!UploadInChunks(image)) {\n'
     '        auto staging = runtime.UploadStagingBuffer(MapSizeBytes(image));\n'
     '        UploadImageContents(image, staging);\n'
     '        runtime.InsertUploadMemoryBarrier();\n'
     '    }\n',
     '    if (!UploadInChunks(image)) {\n'
     '        bool staged_in_pieces = false;\n'
     '        if constexpr (std::is_same_v<Runtime, Vulkan::TextureCacheRuntime>) {\n'
     '            if (::Eden::StagingChunks::Chunked(MapSizeBytes(image)) &&\n'
     '                False(image.flags & ImageFlagBits::AcceleratedUpload)) {\n'
     '                Common::ScratchBuffer<u8> laid_out(MapSizeBytes(image));\n'
     '                ::Eden::StagingChunks::Host<Runtime> host{std::span<u8>(laid_out.data(), MapSizeBytes(image)), &runtime};\n'
     '                UploadImageContents(image, host);\n'
     '                ::Eden::StagingChunks::Said(MapSizeBytes(image));\n'
     '                staged_in_pieces = true;\n'
     '            }\n'
     '        }\n'
     '        if (!staged_in_pieces) {\n'
     '            auto staging = runtime.UploadStagingBuffer(MapSizeBytes(image));\n'
     '            UploadImageContents(image, staging);\n'
     '        }\n'
     '        runtime.InsertUploadMemoryBarrier();\n'
     '    }\n'),
    # The heap-laid-out data has no staging buffer for the accelerated paths: those are kept out of
    # it by the caller, and compiled only for a real staging buffer.
    ('    const GPUVAddr gpu_addr = image.gpu_addr;\n\n'
     '    if (True(image.flags & ImageFlagBits::AcceleratedUpload) && image.host_level_offset != 0) {\n',
     '    const GPUVAddr gpu_addr = image.gpu_addr;\n\n'
     '    if constexpr (!::Eden::StagingChunks::kIsHost<StagingBuffer>) {\n'
     '    if (True(image.flags & ImageFlagBits::AcceleratedUpload) && image.host_level_offset != 0) {\n'),
    ('        runtime.AccelerateImageUpload(image, staging, uploads, 0, 0);\n'
     '        return;\n'
     '    }\n\n'
     '    Tegra::Memory::GpuGuestMemory<u8, Tegra::Memory::GuestMemoryFlags::UnsafeRead> swizzle_data(\n',
     '        runtime.AccelerateImageUpload(image, staging, uploads, 0, 0);\n'
     '        return;\n'
     '    }\n'
     '    }\n\n'
     '    Tegra::Memory::GpuGuestMemory<u8, Tegra::Memory::GuestMemoryFlags::UnsafeRead> swizzle_data(\n'),
    ('        ConvertImage(unswizzle_data_buffer, image.info, mapped_span, copies);\n'
     '        image.UploadMemory(staging, copies);\n',
     '        ConvertImage(unswizzle_data_buffer, image.info, mapped_span, copies);\n'
     '        ::Eden::StagingChunks::UploadStaged(image, staging, copies);\n'),
    ('        const auto copies = UnswizzleImage(*gpu_memory, gpu_addr, image.info, swizzle_data,\n'
     '                                          mapped_span, 0, &unswizzle_worker);\n'
     '        image.UploadMemory(staging, copies);\n',
     '        const auto copies = UnswizzleImage(*gpu_memory, gpu_addr, image.info, swizzle_data,\n'
     '                                          mapped_span, 0, &unswizzle_worker);\n'
     '        ::Eden::StagingChunks::UploadStaged(image, staging, copies);\n'),
    # An ASTC texture decoded in the background is in the heap already: staged in pieces too.
    ('        auto staging = runtime.UploadStagingBuffer(MapSizeBytes(image));\n'
     '        std::memcpy(staging.mapped_span.data(), async_decode->decoded_data.data(),\n'
     '                    async_decode->decoded_data.size());\n'
     '        image.UploadMemory(staging, async_decode->copies);\n',
     '        bool staged_in_pieces = false;\n'
     '        if constexpr (std::is_same_v<Runtime, Vulkan::TextureCacheRuntime>) {\n'
     '            if (::Eden::StagingChunks::Chunked(MapSizeBytes(image))) {\n'
     '                ::Eden::StagingChunks::Host<Runtime> host{\n'
     '                    std::span<u8>(async_decode->decoded_data.data(), async_decode->decoded_data.size()), &runtime};\n'
     '                ::Eden::StagingChunks::UploadStaged(image, host, async_decode->copies);\n'
     '                ::Eden::StagingChunks::Said(MapSizeBytes(image));\n'
     '                staged_in_pieces = true;\n'
     '            }\n'
     '        }\n'
     '        if (!staged_in_pieces) {\n'
     '            auto staging = runtime.UploadStagingBuffer(MapSizeBytes(image));\n'
     '            std::memcpy(staging.mapped_span.data(), async_decode->decoded_data.data(),\n'
     '                        async_decode->decoded_data.size());\n'
     '            image.UploadMemory(staging, async_decode->copies);\n'
     '        }\n'),
    (gc_original, gc),
    # RADV reports all Vulkan allocations, not just cached textures. A game's
    # measured ~1.9 GiB working set triggered dirty eviction at the old 1.6 GiB
    # threshold despite a 4 GiB budget. Retain 40% headroom; keep the original
    # critical threshold and minimum 1 GiB spacing. OpenGL remains unchanged.
    ('const s64 min_vacancy_expected = (6 * mem_threshold) / 10;',
     'const s64 min_vacancy_expected =\n            ((std::is_same_v<Runtime, Vulkan::TextureCacheRuntime> ? 4 : 6) * mem_threshold) / 10;'),
] + texture_costs)

# The four calls inside bind_geometry and the three inside bind_update. They sit in buffer_cache.h
# rather than in Configure, and both spend their time in SynchronizeBuffer's page walk over the
# memory tracker - once for the index buffer, once per enabled vertex buffer, once per per-stage
# segment, every draw, dirty or not. Which of the seven that is cannot be decided by reading it:
# the texture-buffer loop looked like the cost of bind and measured 2.8%.
adapt('src/video_core/buffer_cache/buffer_cache.h',
      'vulkan-cache/video_core/buffer_cache/buffer_cache.h', [
    ('#include "video_core/buffer_cache/buffer_cache_base.h"',
     '#include "video_core/buffer_cache/buffer_cache_base.h"\n#include "performance.h"\n#include "staging_chunks.h"\n'
     'namespace Vulkan { class BufferCacheRuntime; }'),
    # A buffer download of more than 64 MiB is staged in pieces (headless/staging_chunks.h, after
    # Nightshade 07105769); uploads are batched by the core already (MappedUploadMemory). Vulkan
    # only: this file is compiled for OpenGL too.
    ('    if constexpr (USE_MEMORY_MAPS) {\n'
     '        auto download_staging = runtime.DownloadStagingBuffer(total_size_bytes);\n'
     '        const u8* const mapped_memory = download_staging.mapped_span.data();\n',
     '    if constexpr (USE_MEMORY_MAPS && std::is_same_v<Runtime, Vulkan::BufferCacheRuntime>) {\n'
     '        if (::Eden::StagingChunks::Chunked(total_size_bytes)) {\n'
     '            std::vector<BufferCopy> batch;\n'
     '            ::Eden::StagingChunks::StageBuffer(\n'
     '                copies.size(), [&](std::size_t i) { return std::pair<u64, u64>{copies[i].src_offset, copies[i].size}; },\n'
     '                [&](u64 bytes) { return runtime.DownloadStagingBuffer(bytes); },\n'
     '                [&](auto& staging, const std::vector<::Eden::StagingChunks::Piece>& pieces) {\n'
     '                    batch.clear();\n'
     '                    for (const auto& piece : pieces) {\n'
     '                        batch.push_back(BufferCopy{.src_offset = piece.at,\n'
     '                                                   .dst_offset = staging.offset + piece.staged, .size = piece.size});\n'
     '                        buffer.MarkUsage(piece.at, piece.size);\n'
     '                    }\n'
     '                    runtime.CopyBuffer(staging.buffer, buffer, std::span<BufferCopy>(batch.data(), batch.size()), true);\n'
     '                    runtime.Finish();\n'
     '                    for (const auto& piece : pieces) {\n'
     '                        device_memory.WriteBlockUnsafe(buffer.CpuAddr() + piece.at,\n'
     '                                                       staging.mapped_span.data() + piece.staged, piece.size);\n'
     '                    }\n'
     '                });\n'
     '            ::Eden::StagingChunks::Said(total_size_bytes);\n'
     '            return;\n'
     '        }\n'
     '    }\n'
     '    if constexpr (USE_MEMORY_MAPS) {\n'
     '        auto download_staging = runtime.DownloadStagingBuffer(total_size_bytes);\n'
     '        const u8* const mapped_memory = download_staging.mapped_span.data();\n'),
    ('void BufferCache<P>::BindHostGeometryBuffers(bool is_indexed) {',
     'void BufferCache<P>::BindHostGeometryBuffers(bool is_indexed) {\n'
     '    long long since_geo = ::Eden::Performance::MarkStart();'),
    ('    BindHostVertexBuffers();\n'
     '    BindHostTransformFeedbackBuffers();\n'
     '    if (current_draw_indirect) {\n'
     '        BindHostDrawIndirectBuffers();\n'
     '    }\n'
     '}',
     '    ::Eden::Performance::Mark(::Eden::Performance::geo_index, since_geo);\n'
     '    BindHostVertexBuffers();\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::geo_vertex, since_geo);\n'
     '    BindHostTransformFeedbackBuffers();\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::geo_xf, since_geo);\n'
     '    if (current_draw_indirect) {\n'
     '        BindHostDrawIndirectBuffers();\n'
     '    }\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::geo_indirect, since_geo);\n'
     '}'),
    ('void BufferCache<P>::DoUpdateGraphicsBuffers(bool is_indexed) {',
     'void BufferCache<P>::DoUpdateGraphicsBuffers(bool is_indexed) {\n'
     '    long long since_upd = ::Eden::Performance::MarkStart();'),
    ('        UpdateTransformFeedbackBuffers();\n'
     '        for (size_t stage = 0; stage < NUM_STAGES; ++stage) {\n'
     '            UpdateUniformBuffers(stage);\n'
     '            UpdateStorageBuffers(stage);\n'
     '            UpdateTextureBuffers(stage);\n'
     '        }\n'
     '        if (current_draw_indirect) {\n'
     '            UpdateDrawIndirect();\n'
     '        }',
     '        UpdateTransformFeedbackBuffers();\n'
     '        ::Eden::Performance::Mark(::Eden::Performance::upd_state, since_upd);\n'
     '        for (size_t stage = 0; stage < NUM_STAGES; ++stage) {\n'
     '            UpdateUniformBuffers(stage);\n'
     '            UpdateStorageBuffers(stage);\n'
     '            UpdateTextureBuffers(stage);\n'
     '        }\n'
     '        ::Eden::Performance::Mark(::Eden::Performance::upd_stage, since_upd);\n'
     '        if (current_draw_indirect) {\n'
     '            UpdateDrawIndirect();\n'
     '        }\n'
     '        ::Eden::Performance::Mark(::Eden::Performance::upd_indirect, since_upd);'),
])

# These costs sit outside vkCreateGraphicsPipelines: guest shader translation,
# worker readiness, and background serialization can block different threads.
# The six phases inside Configure. draw_config is all of Configure but SetEngine's two pointer
# assignments, and ConfigureImpl is ~210 lines; without these the 2.78 us a draw spends
# configuring arrives as one number with nothing to aim at. One running clock, marked at the
# boundaries the source already has.
configure_phases = [
    ('bool GraphicsPipeline::ConfigureImpl(bool is_indexed) {',
     'bool GraphicsPipeline::ConfigureImpl(bool is_indexed) {\n'
     '    long long since = ::Eden::Performance::MarkStart();'),
    ('    buffer_cache.SetUniformBuffersState(enabled_uniform_buffer_masks, &uniform_buffer_sizes);',
     '    buffer_cache.SetUniformBuffersState(enabled_uniform_buffer_masks, &uniform_buffer_sizes);\n'
     '    // Sampler and image tables synced, uniform-buffer state set.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_sync, since);'),
    ('        texture_cache.FillImageViews(std::span(views.data(), views.size()), false,\n'
     '                                     Spec::has_images);\n    }\n',
     '        texture_cache.FillImageViews(std::span(views.data(), views.size()), false,\n'
     '                                     Spec::has_images);\n    }\n'
     '    // Every texture handle read out of guest memory, every sampler and view filled.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_read, since);\n'
     '    long long since_bind = ::Eden::Performance::MarkStart();'),
    ('    if (regs.transform_feedback_enabled != 0) {',
     '    // The texture-buffer loop over every enabled stage.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::bind_tbo, since_bind);\n'
     '    if (regs.transform_feedback_enabled != 0) {'),
    ('    buffer_cache.UpdateGraphicsBuffers(is_indexed);',
     '    buffer_cache.UpdateGraphicsBuffers(is_indexed);\n'
     '    // Index, vertex, transform-feedback and per-stage buffer updates done.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::bind_update, since_bind);'),
    ('    buffer_cache.BindHostGeometryBuffers(is_indexed);',
     '    buffer_cache.BindHostGeometryBuffers(is_indexed);\n'
     '    // Storage, texture and geometry buffers bound.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::bind_geometry, since_bind);\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_bind, since);'),
    ('    texture_cache.UpdateRenderTargets(false);',
     '    // Descriptor queue acquired, image descriptors pushed, uploads barriered.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_desc, since);\n'
     '    texture_cache.UpdateRenderTargets(false);'),
    ('    if (IsBuilt() && (!pipeline || build->Failed())) {',
     '    // Render targets current, feedback loops checked.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_rt, since);\n'
     '    if (IsBuilt() && (!pipeline || build->Failed())) {'),
    ('    return ConfigureDraw(rescaling, render_area);',
     '    // Compare the descriptor payload against last draw\'s and record the bindings.\n'
     '    const bool configured = ConfigureDraw(rescaling, render_area);\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_draw, since);\n'
     '    return configured;'),
]
for name in ('vk_graphics_pipeline', 'vk_compute_pipeline'):
    # The draw's or dispatch's wait for its pipeline's build: Lichii's graphics pipelines wait in
    # their build (build->Wait), compute ones still on the build mutex.
    build_wait = ('            build->Wait(!worker_only_build);' if name == 'vk_graphics_pipeline'
                  else '            std::unique_lock lock{build_mutex};')
    anchors = [
        ('#include <algorithm>', '#include <algorithm>\n#include "performance.h"'),
        (build_wait, '            auto timer = Eden::Performance::VulkanTimer(16);\n' + build_wait),
    ]
    if name == 'vk_graphics_pipeline':
        anchors += configure_phases
    else:
        # A compute pipeline the driver takes over a second to build is written to stderr (with its
        # hash, as named in logs/native-shaders); EDEN_COMPUTE_BUILD_TRACE=1 also writes each start,
        # so a build that never ends shows as a start with no end.
        anchors.append(('#include <algorithm>\n#include "performance.h"',
                        '#include <algorithm>\n#include <chrono>\n#include <cstdio>\n#include <cstdlib>\n'
                        '#include "performance.h"'))
        anchors.append((
            '            pipeline = device.GetLogical().CreateComputePipeline(compute_ci, *pipeline_cache);\n',
            '            static const bool trace_builds = [] {\n'
            '                const char* value = std::getenv("EDEN_COMPUTE_BUILD_TRACE");\n'
            '                return value != nullptr && value[0] == \'1\';\n'
            '            }();\n'
            '            if (trace_builds) {\n'
            '                std::fprintf(stderr, "EDEN_COMPUTE_BUILD start %016llx\\n",\n'
            '                             static_cast<unsigned long long>(shader_hash));\n'
            '                std::fflush(stderr);\n'
            '            }\n'
            '            const auto build_start = std::chrono::steady_clock::now();\n'
            '            pipeline = device.GetLogical().CreateComputePipeline(compute_ci, *pipeline_cache);\n'
            '            const auto build_ms = std::chrono::duration_cast<std::chrono::milliseconds>(\n'
            '                std::chrono::steady_clock::now() - build_start).count();\n'
            '            if (trace_builds || build_ms >= 1000) {\n'
            '                std::fprintf(stderr, "EDEN_COMPUTE_BUILD done %016llx %lld ms\\n",\n'
            '                             static_cast<unsigned long long>(shader_hash),\n'
            '                             static_cast<long long>(build_ms));\n'
            '                std::fflush(stderr);\n'
            '            }\n'))
    adapt(f'src/video_core/renderer_vulkan/{name}.cpp', name + '_cost.cpp', anchors)
shader_costs = [
    ('#include <algorithm>', '#include <algorithm>\n#include <cstdio>\n#include <unordered_set>\n'
     '#include "performance.h"\n#include "dev_vulkan.h"\n#include "diagnostics.h"'),
    # Development crash probes: report each pipeline's first use (stage hashes) to klog,
    # which survives a GPU fault that kills the title.
    ('    current_pipeline = pipeline.get();\n    current_pipeline->MarkUsed(frame_count);\n'
     '    return BuiltPipeline(current_pipeline);\n}',
     '    current_pipeline = pipeline.get();\n    current_pipeline->MarkUsed(frame_count);\n'
     '    if (::Eden::DevVulkan::trace_pipelines) {\n'
     '        static std::unordered_set<const void*> traced;\n'
     '        if (traced.insert(current_pipeline).second) {\n'
     '            const auto& h = graphics_key.unique_hashes;\n'
     '            char text[256];\n'
     '            std::snprintf(text, sizeof(text), "G%zu p=%p new=%d va=%llx vb=%llx tc=%llx te=%llx gs=%llx fs=%llx",\n'
     '                          traced.size(), (const void*)current_pipeline, int(is_new), (unsigned long long)h[0], (unsigned long long)h[1],\n'
     '                          (unsigned long long)h[2], (unsigned long long)h[3], (unsigned long long)h[4],\n'
     '                          (unsigned long long)h[5]);\n'
     '            ::Eden::Report("pipeline", text);\n'
     '            LOG_INFO(Render_Vulkan, "EDEN_PIPELINE_FIRST_USE {}", text);\n'
     '        }\n'
     '    }\n'
     '    return BuiltPipeline(current_pipeline);\n}'),
    # (Lichii #551 rebuilds a translation whose pointer slots were null between these: the
    # anchor is the return alone.)
    ('    return entry.Get(kepler_compute->IsNativeMode(),\n'
     '                     kepler_compute->regs.code_loc.Address() + qmd.program_start, key);\n}',
     '    ComputePipeline* const pipeline = entry.Get(kepler_compute->IsNativeMode(),\n'
     '        kepler_compute->regs.code_loc.Address() + qmd.program_start, key);\n'
     '    if (::Eden::DevVulkan::trace_pipelines && pipeline) {\n'
     '        static std::unordered_set<const void*> traced;\n'
     '        if (traced.insert(pipeline).second) {\n'
     '            char text[160];\n'
     '            std::snprintf(text, sizeof(text), "C%zu p=%p new=%d cs=%llx shared=%u wg=%u,%u,%u", traced.size(),\n'
     '                          (const void*)pipeline, int(is_new), (unsigned long long)key.unique_hash, unsigned(key.shared_memory_size),\n'
     '                          unsigned(key.workgroup_size[0]), unsigned(key.workgroup_size[1]),\n'
     '                          unsigned(key.workgroup_size[2]));\n'
     '            ::Eden::Report("pipeline", text);\n'
     '            LOG_INFO(Render_Vulkan, "EDEN_PIPELINE_FIRST_USE {}", text);\n'
     '        }\n'
     '    }\n'
     '    return pipeline;\n}'),
    ('std::unique_ptr<GraphicsPipeline> PipelineCache::CreateGraphicsPipeline() {',
     'std::unique_ptr<GraphicsPipeline> PipelineCache::CreateGraphicsPipeline() {\n    auto timer = Eden::Performance::VulkanTimer(17);'),
    ('                                                               std::span<const u64> keep_pointers) {',
     '                                                               std::span<const u64> keep_pointers) {\n    auto timer = Eden::Performance::VulkanTimer(17);'),
    ('                                                 u32 cache_version) try {',
     '                                                 u32 cache_version) try {\n    auto timer = Eden::Performance::VulkanTimer(18);'),
    ('        device.SupportsTransformFeedbackProvokingVertexPreservation();\n}\n',
     '        device.SupportsTransformFeedbackProvokingVertexPreservation();\n'
     '    if (Eden::Performance::vulkan_cost_enabled.load(std::memory_order_relaxed))\n'
     '        std::printf("EDEN_VULKAN_DYNAMIC blend=%u enables=%u vertex=%u\\n",\n'
     '                    unsigned(dynamic_features.has_extended_dynamic_state_3_blend),\n'
     '                    unsigned(dynamic_features.has_extended_dynamic_state_3_enables),\n'
     '                    unsigned(dynamic_features.has_dynamic_vertex_input));\n'
     '}\n'),
]
# Time both graphics and compute preparation, without modifying the compiler or
# reading guest state on another thread. These intervals overlap shader_prepare.
shader_source = (source / 'src/video_core/renderer_vulkan/vk_pipeline_cache.cpp').read_text()
shader_source = shader_source.replace('    auto it{graphics_cache.find(graphics_key)};\n    if (it == graphics_cache.end()) {',
                                      '    auto it{graphics_cache.find(graphics_key)};\n    const bool is_new = it == graphics_cache.end();\n    if (is_new) {')
for expression, index, count in (
    ('main_pools.ReleaseContents()', 19, 2),
    ('TranslateProgram(pools.inst, pools.block, env, cfg, host_info)', 21, 3),
    ('EmitSPIRV(profile, runtime_info, program, binding)', 22, 1),
    ('EmitSPIRV(profile, program)', 22, 1),
    ('BuildShader(device, code)', 23, 2),
):
    if shader_source.count(expression) != count:
        raise RuntimeError(f'Shader cost anchor changed: {expression}')
    shader_source = shader_source.replace(expression,
        f'([&] {{ auto cost = Eden::Performance::VulkanTimer({index}); '
        f'return {expression}; }}())')
for statement in (
    'Shader::Maxwell::Flow::CFG cfg(env, pools.flow_block, cfg_offset, index == 0);',
    'Shader::Maxwell::Flow::CFG cfg{env, pools.flow_block, env.StartAddress()};',
):
    expected = 2 if 'cfg_offset' in statement else 1  # graphics exception dump too
    if shader_source.count(statement) != expected:
        raise RuntimeError(f'Shader CFG anchor changed: {statement}')
    shader_source = shader_source.replace(statement,
        'auto cfg_cost = Eden::Performance::VulkanTimer(20);\n        ' + statement +
        '\n        cfg_cost.reset();')
# EDEN_COMPUTE_SPIRV_DUMP=<dir>: each compute shader's SPIR-V, written as <hash>.spv before the
# driver builds it, so a build that never ends (EDEN_COMPUTE_BUILD start with no done) can be
# compiled off the console.
shader_costs.append((
    '    // Text log + .spv dump. Same split as the graphics path.',
    '    if (static const char* const spirv_dir = std::getenv("EDEN_COMPUTE_SPIRV_DUMP"); spirv_dir) {\n'
    '        char spirv_path[512];\n'
    '        std::snprintf(spirv_path, sizeof(spirv_path), "%s/%016llx.spv", spirv_dir,\n'
    '                      static_cast<unsigned long long>(key.unique_hash));\n'
    '        if (std::FILE* spirv_file = std::fopen(spirv_path, "wb")) {\n'
    '            std::fwrite(code.data(), sizeof(u32), code.size(), spirv_file);\n'
    '            std::fclose(spirv_file);\n'
    '        }\n'
    '    }\n'
    '    // Text log + .spv dump. Same split as the graphics path.'))
# Pipeline builders: at most 4 on the console. Each holds a pipeline's compiler memory, and a
# session that starts with an empty shader cache compiles on all of them while the game runs:
# Ocarina's first session on Lichii 09b78bd2df, with its mesh shaders compiled as compute,
# reached 1.9 GiB of heap in use in compiles (1.2 before) with the guest holding 4.9 GiB, and an
# allocation in the compiler failed. Fewer builders: slower first compiles, a bounded peak.
shader_costs.append((
    '#else\n    return max_core_threads;\n#endif\n}',
    '#else\n    return std::min<size_t>(max_core_threads, 4);\n#endif\n}'))
for old, new in shader_costs:
    if shader_source.count(old) != 1:
        raise RuntimeError(f'Shader pipeline anchor changed: {old[:60]}')
    shader_source = shader_source.replace(old, new)
shader_output = output / 'vk_pipeline_cache_cost.cpp'
if not shader_output.exists() or shader_output.read_text() != shader_source:
    shader_output.write_text(shader_source)

# Scope only the existing synchronous operations; never alter their ordering.
# Report these caller costs separately from the overlapping API totals.
import re
# Development GPU fault probes (dev_vulkan.h): multi-range storage buffers are
# the one path that binds sparse ranges of the console's device memory.
probes = {
    'vk_multi_range_buffer': [
        ('#include <algorithm>', '#include <algorithm>\n#include <cstdio>\n#include "dev_vulkan.h"\n#include "diagnostics.h"'),
        ('    if (bind_result != VK_SUCCESS) {\n        return SparseBuffer{};\n    }',
         '    if (bind_result != VK_SUCCESS) {\n'
         '        if (::Eden::DevVulkan::trace_pipelines) ::Eden::Report("multirange", "sparse bind failed");\n'
         '        return SparseBuffer{};\n    }'),
        ('    retired.push_back(Retired{',
         '    if (::Eden::DevVulkan::trace_pipelines) {\n'
         '        static unsigned retires = 0;\n'
         '        if (++retires <= 64) {\n'
         '            char text[96];\n'
         '            std::snprintf(text, sizeof(text), "retire #%u sparse=%d tick=%llu", retires,\n'
         '                          int(bool(entry.sparse_handle)), (unsigned long long)scheduler.CurrentTick());\n'
         '            ::Eden::Report("multirange", text);\n'
         '        }\n'
         '    }\n'
         '    retired.push_back(Retired{'),
        ('    entries.emplace(key, std::move(entry));',
         '    if (::Eden::DevVulkan::trace_pipelines) {\n'
         '        static unsigned created = 0;\n'
         '        if (++created <= 256) {\n'
         '            char text[192];\n'
         '            std::snprintf(text, sizeof(text), "new #%u key=%llx total=%llu sources=%zu sparse=%d addr=%llx",\n'
         '                          created, (unsigned long long)key, (unsigned long long)total, sources.size(),\n'
         '                          int(bool(entry.sparse_handle)), (unsigned long long)entry.address);\n'
         '            ::Eden::Report("multirange", text);\n'
         '        }\n'
         '    }\n'
         '    entries.emplace(key, std::move(entry));'),
    ],
    'vk_buffer_cache': [
        ('#include <algorithm>', '#include <algorithm>\n#include "dev_vulkan.h"'),
        ('bool BufferCacheRuntime::BindMultiRangeStorageBuffer(u64 key, bool is_written) {\n',
         'bool BufferCacheRuntime::BindMultiRangeStorageBuffer(u64 key, bool is_written) {\n'
         '    if (::Eden::DevVulkan::disable_multi_range) {\n        return false;\n    }\n'),
    ],
    # With custom border colours off (the console default), report which clamp-to-border
    # samplers ask for a colour the three standard ones cannot give exactly.
    'vk_texture_cache': [
        ('#include <algorithm>', '#include <algorithm>\n#include <cstdio>\n#include "dev_vulkan.h"\n#include "diagnostics.h"'),
        ('    has_custom_border_colors = samples_border && device.IsCustomBorderColorUsable();',
         '    if (::Eden::DevVulkan::trace_pipelines && samples_border) {\n'
         '        static std::vector<std::array<float, 4>> reported;\n'
         '        const auto report = [](const std::array<float, 4>& c, const char* kind) {\n'
         '            if (c == std::array<float, 4>{0, 0, 0, 0} || c == std::array<float, 4>{0, 0, 0, 1} ||\n'
         '                c == std::array<float, 4>{1, 1, 1, 1} || reported.size() >= 32 ||\n'
         '                std::find(reported.begin(), reported.end(), c) != reported.end()) return;\n'
         '            reported.push_back(c);\n'
         '            char text[128];\n'
         '            std::snprintf(text, sizeof(text), "non-standard %s border %g %g %g %g", kind, c[0], c[1],\n'
         '                          c[2], c[3]);\n'
         '            ::Eden::Report("sampler", text);\n'
         '        };\n'
         '        report(border_color, "linear");\n'
         '        if (tsc.srgb_conversion) report(srgb_border_color, "srgb");\n'
         '    }\n'
         '    has_custom_border_colors = samples_border && device.IsCustomBorderColorUsable();'),
    ],
}
for name, index, expected in (
    ('vk_fence_manager', 6, 1), ('vk_buffer_cache', 7, 4),
    ('vk_texture_cache', 8, 2), ('vk_swapchain', 9, 6),
    ('vk_descriptor_buffer', 10, 2), ('vk_multi_range_buffer', 11, 1),
    ('vk_compute_pass', 12, 1),
):
    relative = f'src/video_core/renderer_vulkan/{name}.cpp'
    destination = 'vulkan_swapchain.cpp' if name == 'vk_swapchain' else name + '_cost.cpp'
    # Preserve the earlier swapchain error-recovery adaptation.
    path = output / destination
    text = path.read_text() if name == 'vk_swapchain' else (source / relative).read_text()
    pattern = r'(?m)^( +)(scheduler\.(?:Wait|Finish)\([^\n]*\);)$'
    matches = list(re.finditer(pattern, text))
    if len(matches) != expected:
        raise RuntimeError(f'Wait-site anchors changed: {name}: {len(matches)} != {expected}')
    text = re.sub(pattern, lambda m: m[1] + '{ auto timer = Eden::Performance::VulkanTimer('
                  + str(index) + '); ' + m[2] + ' }', text)
    if name == 'vk_compute_pass':
        # ASTC decode has no CPU consumer. Its existing image barrier orders
        # later shaders, and staging/descriptor reuse is protected by ticks.
        text = text.replace('VulkanTimer(12); scheduler.Finish();',
                            'VulkanTimer(12); scheduler.Flush();')
    for probe_old, probe_new in probes.get(name, []):
        if text.count(probe_old) != 1:
            raise RuntimeError(f'Probe anchor changed: {name}: {probe_old[:60]}')
        text = text.replace(probe_old, probe_new)
    text = '#include "performance.h"\n' + text
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
# Eden loads these shaders when the renderer starts, whether frames are to be generated or not:
# the reasons are only written when the setting is on, or every session without the user's
# Lossless.dll would say so.
adapt('src/video_core/renderer_vulkan/present/lsfg_shaders.cpp', 'vulkan_lsfg_shaders.cpp', [
    ('#include "video_core/vulkan_common/vulkan_device.h"',
     '#include <string>\n#include "common/settings.h"\n#include "video_core/vulkan_common/vulkan_device.h"\n#include "diagnostics.h"'),
    ("""    valid = true;""",
     """    valid = true;
    if (wanted)
        Eden::Report("framegen", ("shaders ready: " + std::to_string(code.size()) + " modules").c_str());"""),
])
# Two members, so that "frames are being generated" and "this image cannot take them" are said once
# per session rather than once per process or once per frame: this object is built per renderer,
# and the renderer is built per game session.
adapt('src/video_core/renderer_vulkan/present/frame_gen.h',
      'include/video_core/renderer_vulkan/present/frame_gen.h', [
    ('    bool dumped{};', '    bool dumped{};\n    bool generation_reported{};\n    bool storage_reported{};'),
])
adapt('src/video_core/renderer_vulkan/present/frame_gen.cpp', 'vulkan_frame_gen.cpp', [
    ('#include "video_core/vulkan_common/vulkan_device.h"',
     '#include "video_core/vulkan_common/vulkan_device.h"\n#include "diagnostics.h"'),
    # Lichii logs why frames are not generated (LsfgShaders, FrameGenState); this says that they are.
    ("""    generated = warm && warm_streak >= LSFG_RECURRENCE_FRAMES && plan.generations > 0;""",
     """    generated = warm && warm_streak >= LSFG_RECURRENCE_FRAMES && plan.generations > 0;
    if (generated && !generation_reported) {
        generation_reported = true;
        Eden::Report("framegen",
                     ("generating " + std::to_string(plan.generations) +
                      " extra frame(s) per guest frame").c_str());
    }"""),
])
# Split the dispatch into its three phases (Tegra::Control::Scheduler::Push): the channel lock,
# queueing the submit list, and executing the guest's methods. The dispatch timer wraps the whole
# call from gpu_thread.cpp, so these say which of the three it is spent in.
adapt('src/video_core/control/scheduler.cpp', 'control_scheduler.cpp', [
    ('#include <memory>', '#include <memory>\n#include "performance.h"'),
    ('        std::unique_lock lk(scheduling_guard);\n'
     '        auto it = channels.find(channel);\n'
     '        ASSERT(it != channels.end());\n'
     '        channel_state = it->second;\n'
     '    }\n'
     '    BindChannel(gpu, channel_state->bind_id);\n'
     '    // Process commands outside the lock to reduce contention.\n'
     '    // Multiple channels can prepare their commands in parallel.\n'
     '    channel_state->payload->dma_pusher.Push(std::move(entries));\n'
     '    channel_state->payload->dma_pusher.DispatchCalls();',
     '        const ::Eden::Performance::Timer sched_lock_timer(::Eden::Performance::gpu_sched_lock);\n'
     '        std::unique_lock lk(scheduling_guard);\n'
     '        auto it = channels.find(channel);\n'
     '        ASSERT(it != channels.end());\n'
     '        channel_state = it->second;\n'
     '    }\n'
     '    BindChannel(gpu, channel_state->bind_id);\n'
     '    // Process commands outside the lock to reduce contention.\n'
     '    // Multiple channels can prepare their commands in parallel.\n'
     '    {\n'
     '        const ::Eden::Performance::Timer dma_push_timer(::Eden::Performance::gpu_dma_push);\n'
     '        channel_state->payload->dma_pusher.Push(std::move(entries));\n'
     '    }\n'
     '    {\n'
     '        const ::Eden::Performance::Timer dma_dispatch_timer(::Eden::Performance::gpu_dma_dispatch);\n'
     '        channel_state->payload->dma_pusher.DispatchCalls();\n'
     '    }'),
])
shader_header = '#pragma once\n#include <cstdint>\n'
for stage in ('vert', 'frag'):
    binary = output / f'vulkan_hud.{stage}.spv'
    subprocess.run(['glslangValidator', '-V', '--target-env', 'vulkan1.0',
                    '-o', str(binary), str(port / f'vulkan_hud.{stage}')], check=True)
    subprocess.run(['spirv-val', '--target-env', 'vulkan1.0', str(binary)], check=True)
    data = binary.read_bytes()
    words = struct.unpack('<' + 'I' * (len(data) // 4), data)
    shader_header += f'inline constexpr uint32_t EDEN_HUD_{stage.upper()}_SPV[] = {{\n'
    shader_header += ','.join(hex(word) for word in words) + '\n};\n'
path = output / 'vulkan_hud_shaders.h'
if not path.exists() or path.read_text() != shader_header:
    path.write_text(shader_header)
if broken:
    print(f"{len(broken)} anchor(s) of the Eden source changed; adapt them in tools/prepare-vulkan-port.py:", file=sys.stderr)
    for line in broken:
        print("  " + line, file=sys.stderr)
    sys.exit(1)
