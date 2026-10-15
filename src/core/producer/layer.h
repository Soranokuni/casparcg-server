/*
 * Copyright (c) 2011 Sveriges Television AB <info@casparcg.com>
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
 *
 * Author: Robert Nagy, ronag89@gmail.com
 */

#pragma once

#include "frame_producer.h"
#include "layer_item.h"

#include "../fwd.h"
#include "../monitor/monitor.h"

#include <common/memory.h>

#include <vector>

namespace caspar { namespace core {

class layer final
{
    layer(const layer&);
    layer& operator=(const layer&);

  public:
    explicit layer(const core::video_format_desc format_desc);
    layer(layer&& other);

    layer& operator=(layer&& other);

    void swap(layer& other);

    // clear_queue drops the waiting items first (PLAY and LOAD with a producer are takes).
    void load(spl::shared_ptr<frame_producer> producer,
              bool                            preview,
              bool                            auto_play   = false,
              item_info                       info        = {},
              bool                            clear_queue = false);
    void play();
    void preview();
    void pause();
    void resume();
    void stop();

    // PlayOut fork: runs once per tick, before receive(). Promotes an AUTO background, reports the
    // end of the foreground and fails over on a stall. Returns the actions of the items that went
    // on air since the last call.
    std::vector<std::shared_ptr<item_actions>> prepare();

    draw_frame receive(const video_field field, int nb_samples);
    draw_frame receive_background(const video_field field, int nb_samples);

    core::monitor::state state() const;

    spl::shared_ptr<frame_producer> foreground() const;
    spl::shared_ptr<frame_producer> background() const;
    bool                            has_background() const;

    // PlayOut fork: the wait queue. The background is the head of the queue: when it goes on air,
    // the next queued item moves into the background.
    enum class attach_result
    {
        not_found,
        attached,
        on_air,
    };

    const std::wstring& foreground_tag() const;
    void                queue_push(spl::shared_ptr<frame_producer> producer, bool auto_play, item_info info);
    // Removes the waiting items after the item tagged after_tag (all of them when after_tag is
    // empty or on air). Returns the number removed, or -1 when after_tag is not on the layer.
    int           queue_clear(const std::wstring& after_tag);
    bool          queue_remove(const std::wstring& tag);
    attach_result attach_actions(const std::wstring& tag, const std::shared_ptr<item_actions>& actions);
    void          set_fallback(const fallback_config& config);
    void          on_clear();
    std::vector<layer_event> take_events();
    layer_snapshot           snapshot() const;

  private:
    struct impl;
    spl::shared_ptr<impl> impl_;
};

}} // namespace caspar::core
