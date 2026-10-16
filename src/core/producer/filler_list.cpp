/*
 * Copyright (c) 2026 PlayOutVue contributors
 *
 * This file is part of CasparCG (www.casparcg.com).
 *
 * CasparCG is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * CasparCG is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with CasparCG. If not, see <http://www.gnu.org/licenses/>.
 */

#include "../StdAfx.h"

#include "filler_list.h"

#include <common/except.h>
#include <common/log.h>
#include <common/utf.h>

#include <boost/filesystem/path.hpp>

#include <algorithm>
#include <chrono>
#include <deque>
#include <mutex>
#include <random>
#include <thread>

namespace caspar { namespace core {

namespace {

// After every file failed, wait this long before trying the folder again.
const std::chrono::seconds retry_after_exhausted(5);

// A filler whose first frame is not decoded by then counts as broken.
const std::chrono::seconds first_frame_timeout(1);

std::wstring file_name(const std::wstring& path) { return boost::filesystem::path(path).filename().wstring(); }

} // namespace

// Shared with the worker thread, which may outlive the list by the time it takes to open one file.
struct filler_list::impl
{
    const std::vector<std::wstring> files;
    const bool                      shuffle;
    const int                       norepeat;
    const factory_t                 factory;

    mutable std::mutex                    mutex;
    std::mt19937                          rng{std::random_device{}()};
    std::size_t                           cursor = 0;      // next file in order (no shuffle)
    std::deque<std::size_t>               recent;          // the last picks, newest last (shuffle)
    std::uint64_t                         picks   = 0;     // fillers handed out
    bool                                  closed  = false; // the list is gone
    bool                                  pending = false;
    std::optional<pick>                   ready;
    bool                                  is_exhausted = false;
    std::chrono::steady_clock::time_point retry_at;
    std::vector<failure>                  failures;

    impl(std::vector<std::wstring> f, bool s, int n, factory_t fac)
        : files(std::move(f))
        , shuffle(s)
        , norepeat(std::max(0, std::min(n, static_cast<int>(files.size()) - 1)))
        , factory(std::move(fac))
    {
    }

    // The files to try, best first. Called with the mutex held.
    std::vector<std::size_t> candidates()
    {
        std::vector<std::size_t> order;
        if (!shuffle) {
            for (std::size_t n = 0; n < files.size(); ++n) {
                order.push_back((cursor + n) % files.size());
            }
            return order;
        }

        std::vector<std::size_t> allowed;
        for (std::size_t n = 0; n < files.size(); ++n) {
            if (std::find(recent.begin(), recent.end(), n) == recent.end()) {
                allowed.push_back(n);
            }
        }
        std::shuffle(allowed.begin(), allowed.end(), rng);
        order = allowed;
        // Only when every allowed file fails: the recent ones, the oldest first.
        order.insert(order.end(), recent.begin(), recent.end());
        return order;
    }

    bool is_closed() const
    {
        std::lock_guard<std::mutex> lock(mutex);
        return closed;
    }

    void picked(std::size_t index)
    {
        cursor = (index + 1) % files.size();
        recent.push_back(index);
        while (recent.size() > static_cast<std::size_t>(norepeat)) {
            recent.pop_front();
        }
    }

    static void open_next(const std::shared_ptr<impl>& self, std::vector<std::size_t> order)
    {
        for (auto index : order) {
            const auto& path = self->files[index];
            std::string reason;
            try {
                auto producer = self->factory(path);
                // A file that ffmpeg cannot read still makes a producer: it never delivers a frame.
                // Offer a filler only once its first frame is decoded, so the cut to it is gapless.
                const auto deadline = std::chrono::steady_clock::now() + first_frame_timeout;
                while (producer != frame_producer::empty() && !producer->is_ready() &&
                       std::chrono::steady_clock::now() < deadline && !self->is_closed()) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                }
                if (producer != frame_producer::empty() && producer->is_ready()) {
                    std::lock_guard<std::mutex> lock(self->mutex);
                    if (self->closed) {
                        self->pending = false;
                        return;
                    }
                    self->picked(index);
                    self->ready        = pick{std::move(producer), file_name(path), 0};
                    self->is_exhausted = false;
                    self->pending      = false;
                    return;
                }
                if (self->is_closed()) {
                    std::lock_guard<std::mutex> lock(self->mutex);
                    self->pending = false;
                    return;
                }
                reason = producer == frame_producer::empty() ? "FALLBACK LIST 404" : "FALLBACK LIST 500";
            } catch (file_not_found&) {
                reason = "FALLBACK LIST 404";
            } catch (...) {
                CASPAR_LOG_CURRENT_EXCEPTION();
                reason = "FALLBACK LIST 500";
            }

            CASPAR_LOG(warning) << L"[fallback] Filler " << path << L" could not be opened (" << u16(reason)
                                << L"). Trying the next one.";
            std::lock_guard<std::mutex> lock(self->mutex);
            self->failures.push_back({file_name(path), reason});
            if (self->closed) {
                self->pending = false;
                return;
            }
        }

        CASPAR_LOG(warning) << L"[fallback] No filler of the list could be opened.";
        std::lock_guard<std::mutex> lock(self->mutex);
        self->is_exhausted = true;
        self->retry_at     = std::chrono::steady_clock::now() + retry_after_exhausted;
        self->pending      = false;
    }

    // Called with the mutex held.
    void start_warm(const std::shared_ptr<impl>& self)
    {
        if (closed || pending || ready || files.empty())
            return;
        if (is_exhausted && std::chrono::steady_clock::now() < retry_at)
            return;

        pending = true;
        std::thread([self, order = candidates()]() mutable { open_next(self, std::move(order)); }).detach();
    }
};

filler_list::filler_list(std::vector<std::wstring> files, bool shuffle, int norepeat, factory_t factory)
    : impl_(std::make_shared<impl>(std::move(files), shuffle, norepeat, std::move(factory)))
{
}

filler_list::~filler_list()
{
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->closed = true;
    impl_->ready.reset();
}

std::optional<filler_list::pick> filler_list::take()
{
    std::lock_guard<std::mutex> lock(impl_->mutex);

    std::optional<pick> result;
    if (impl_->ready && impl_->ready->producer->is_ready()) {
        result = std::move(impl_->ready);
        impl_->ready.reset();
        result->n = ++impl_->picks;
    }
    impl_->start_warm(impl_);
    return result;
}

void filler_list::warm()
{
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->start_warm(impl_);
}

bool filler_list::exhausted() const
{
    std::lock_guard<std::mutex> lock(impl_->mutex);
    return impl_->is_exhausted;
}

std::vector<filler_list::failure> filler_list::take_failures()
{
    std::lock_guard<std::mutex> lock(impl_->mutex);
    std::vector<failure>        result;
    result.swap(impl_->failures);
    return result;
}

std::size_t filler_list::size() const { return impl_->files.size(); }
int         filler_list::norepeat() const { return impl_->norepeat; }
bool        filler_list::shuffle() const { return impl_->shuffle; }

}} // namespace caspar::core
