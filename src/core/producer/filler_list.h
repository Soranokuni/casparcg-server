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

// PlayOut fork: the filler list of FALLBACK LIST. See PLAYOUT-FORK.md in the repository root.

#pragma once

#include "frame_producer.h"

#include <common/memory.h>

#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace caspar { namespace core {

// The files of a filler folder, played one after the other. The producer of the next filler is
// opened ahead of time on a worker thread, so the cut to it is gapless and never blocks a tick.
class filler_list final
{
  public:
    using factory_t = std::function<spl::shared_ptr<frame_producer>(const std::wstring& path)>;

    struct pick
    {
        spl::shared_ptr<frame_producer> producer = frame_producer::empty();
        std::wstring                    file;  // the file name, without the folder
        std::uint64_t                   n = 0; // 1 for the first filler on air, then 2, 3, ...
    };

    struct failure
    {
        std::wstring file;
        std::string  reason; // "FALLBACK LIST 404" (not found / no producer) or "FALLBACK LIST 500" (failed to open)
    };

    // files: full paths, in the order to play them when not shuffled.
    // norepeat: a file does not come back within this many picks (shuffle only; clamped to files - 1).
    filler_list(std::vector<std::wstring> files, bool shuffle, int norepeat, factory_t factory);
    ~filler_list();

    filler_list(const filler_list&)            = delete;
    filler_list& operator=(const filler_list&) = delete;

    // Stage thread. The warm producer, when it is open and has a frame; nothing otherwise (try again
    // next tick). Starts opening the next one.
    std::optional<pick> take();

    // Starts opening the next filler if none is open or being opened.
    void warm();

    // Every file failed in the last attempt. A new attempt starts at most every few seconds.
    bool exhausted() const;

    // Files that failed to open since the last call.
    std::vector<failure> take_failures();

    std::size_t size() const;
    int         norepeat() const;
    bool        shuffle() const;

  private:
    struct impl;
    std::shared_ptr<impl> impl_;
};

}} // namespace caspar::core
