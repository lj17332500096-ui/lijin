/* taskStore.js — 订阅者式任务状态仓库。
 * 只依赖 eventReducer + api：所有更新走 ingest（同一入口），组件订阅快照。
 * 生命周期：refresh()（拉 /api/tasks/{id}）→ loadEvents()（REST 回放）→ stream 接入事件。 */
(function (global) {
  "use strict";

  var RT = global.RT = global.RT || {};

  function createTaskStore(options) {
    options = options || {};
    var api = options.api || RT.api;
    var snapshots = {};   // id -> snapshot
    var listeners = {};   // id -> Set<fn>
    var activeId = null;
    var stateListeners = []; // 全局订阅（页面切换等）

    function get(id) {
      if (!snapshots[id]) snapshots[id] = RT.eventReducer.emptySnapshot(id);
      return snapshots[id];
    }
    function emit(id) {
      var s = get(id);
      (listeners[id] || []).forEach(function (fn) { try { fn(s); } catch (e) { /* ignore */ } });
      stateListeners.forEach(function (fn) { try { fn(id, s); } catch (e) { /* ignore */ } });
    }

    return {
      /* ---- 订阅 ---- */
      subscribe(id, fn) {
        if (!listeners[id]) listeners[id] = new Set();
        listeners[id].add(fn);
        fn(get(id)); // 立即推送当前快照
        return function () { if (listeners[id]) listeners[id].delete(fn); };
      },
      subscribeAll(fn) {
        stateListeners.push(fn);
        return function () {
          var i = stateListeners.indexOf(fn);
          if (i > -1) stateListeners.splice(i, 1);
        };
      },

      /* ---- 事件入口（stream 与轮询统一走这里） ---- */
      ingest(id, event) {
        if (!event || !event.type) return get(id);
        snapshots[id] = RT.eventReducer.nextState(get(id), event);
        emit(id);
        return snapshots[id];
      },
      ingestMany(id, events) {
        snapshots[id] = RT.eventReducer.applyMany(get(id), events);
        emit(id);
        return snapshots[id];
      },

      /* ---- 任务元数据（Task 字段与快照合并） ---- */
      setTask(task) {
        var id = task.id;
        var s = get(id);
        s.task = task;
        if (!s.state) s.state = task.state;
        snapshots[id] = s;
        emit(id);
        return s;
      },

      /* ---- 远程加载 ---- */
      async refresh(id) {
        var task = await api.getTask(id);
        return this.setTask(task);
      },
      async loadEvents(id) {
        var events = await api.taskEvents(id);
        return this.ingestMany(id, events);
      },

      /* ---- 活动任务 ---- */
      setActive(id) { activeId = id; stateListeners.forEach(function (fn) { try { fn(id, get(id)); } catch (e) {} }); },
      active() { return activeId ? get(activeId) : null; },
      activeId() { return activeId; },
      snapshot: get,
      has(id) { return !!snapshots[id]; },
    };
  }

  RT.taskStore = createTaskStore();
})(window);
