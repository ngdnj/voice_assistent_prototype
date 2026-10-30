// The controller is shared by mouse/touch Pointer Events and can be cancelled on scrolling.
export function createLongPress(onLongPress: () => void, delay = 550) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let pointer: { id: number; x: number; y: number } | undefined;

  function cancel(pointerId?: number) {
    if (pointerId !== undefined && pointer?.id !== pointerId) return;
    clearTimeout(timer);
    timer = undefined;
    pointer = undefined;
  }

  return {
    start(id: number, x: number, y: number) {
      cancel();
      pointer = { id, x, y };
      timer = setTimeout(() => {
        timer = undefined;
        pointer = undefined;
        onLongPress();
      }, delay);
    },
    move(id: number, x: number, y: number) {
      if (pointer?.id === id && Math.hypot(x - pointer.x, y - pointer.y) > 10) cancel();
    },
    cancel,
  };
}
