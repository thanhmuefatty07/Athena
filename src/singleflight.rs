//! Async singleflight: one leader per equal key until completion or cancellation.
//! Complete request semantics (URL, method, tenant/auth, headers varying response,
//! representation, freshness policy, etc.) MUST be encoded into K.
use std::{collections::HashMap, future::Future, hash::Hash, sync::{Arc, Mutex}};
use tokio::sync::watch;

type SharedResult<V, E> = Result<Arc<V>, Arc<E>>;

enum State<V, E> {
    Running,
    Done(SharedResult<V, E>),
    Cancelled,
}

impl<V, E> Clone for State<V, E> {
    fn clone(&self) -> Self {
        match self {
            State::Running => State::Running,
            State::Done(Ok(value)) => State::Done(Ok(Arc::clone(value))),
            State::Done(Err(error)) => State::Done(Err(Arc::clone(error))),
            State::Cancelled => State::Cancelled,
        }
    }
}

struct Flight<V, E> { sender: watch::Sender<State<V, E>> }

#[derive(Debug)]
pub enum FlightError<E> {
    Fetch(Arc<E>),
    Cancelled,
}

pub struct SingleFlight<K, V, E> {
    active: Mutex<HashMap<K, Arc<Flight<V, E>>>>,
}

impl<K, V, E> Default for SingleFlight<K, V, E> {
    fn default() -> Self { Self { active: Mutex::new(HashMap::new()) } }
}

impl<K, V, E> SingleFlight<K, V, E>
where K: Eq + Hash + Clone,
{
    pub fn new() -> Self { Self::default() }

    pub async fn call<F, Fut>(&self, key: K, fetch: F) -> Result<Arc<V>, FlightError<E>>
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = Result<V, E>>,
    {
        let (flight, leader) = {
            let mut table = self.active.lock().unwrap_or_else(|e| e.into_inner());
            match table.get(&key) {
                Some(active) => (Arc::clone(active), false),
                None => {
                    let (sender, _initial_receiver) = watch::channel(State::Running);
                    let created = Arc::new(Flight { sender });
                    table.insert(key.clone(), Arc::clone(&created));
                    (created, true)
                }
            }
        };
        if leader {
            let mut guard = LeaderGuard {
                active: &self.active, key, flight: Arc::clone(&flight), complete: false,
            };
            let answer: SharedResult<V, E> = fetch().await.map(Arc::new).map_err(Arc::new);
            // Unlike send(), send_replace() records a result even with zero receivers.
            flight.sender.send_replace(State::Done(answer.clone()));
            guard.finish();
            answer.map_err(FlightError::Fetch)
        } else {
            let mut rx = flight.sender.subscribe();
            loop {
                // The borrow guard MUST be dropped before awaiting changed().
                let state = { (*rx.borrow()).clone() };
                match state {
                    State::Running => {},
                    State::Done(result) => return result.map_err(FlightError::Fetch),
                    State::Cancelled => return Err(FlightError::Cancelled),
                }
                if rx.changed().await.is_err() { return Err(FlightError::Cancelled); }
            }
        }
    }
}

struct LeaderGuard<'a, K: Eq + Hash, V, E> {
    active: &'a Mutex<HashMap<K, Arc<Flight<V, E>>>>,
    key: K,
    flight: Arc<Flight<V, E>>,
    complete: bool,
}
impl<K: Eq + Hash, V, E> LeaderGuard<'_, K, V, E> {
    fn remove_if_current(&self) {
        let mut table = self.active.lock().unwrap_or_else(|e| e.into_inner());
        if table.get(&self.key).is_some_and(|v| Arc::ptr_eq(v, &self.flight)) {
            table.remove(&self.key);
        }
    }
    fn finish(&mut self) {
        self.remove_if_current();
        self.complete = true;
    }
}
impl<K: Eq + Hash, V, E> Drop for LeaderGuard<'_, K, V, E> {
    fn drop(&mut self) {
        if !self.complete {
            self.flight.sender.send_replace(State::Cancelled);
            self.remove_if_current();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::{sync::Barrier, time::{sleep, Duration}};

    #[tokio::test(flavor = "current_thread")]
    async fn concurrent_joiners_share_one_fetch() {
        let sf = Arc::new(SingleFlight::<String, u32, &'static str>::new());
        let starts = Arc::new(AtomicUsize::new(0));
        let barrier = Arc::new(Barrier::new(32));
        let mut tasks = Vec::new();
        for _ in 0..32 {
            let sf = Arc::clone(&sf);
            let starts = Arc::clone(&starts);
            let barrier = Arc::clone(&barrier);
            tasks.push(tokio::spawn(async move {
                barrier.wait().await;
                sf.call("GET /x tenant=A".to_owned(), || async move {
                    starts.fetch_add(1, Ordering::SeqCst);
                    sleep(Duration::from_millis(40)).await;
                    Ok(777)
                }).await.unwrap()
            }));
        }
        for task in tasks { assert_eq!(*task.await.unwrap(), 777); }
        assert_eq!(starts.load(Ordering::SeqCst), 1);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn a_completed_flight_does_not_cache_a_future_request() {
        let sf = SingleFlight::<u32, u32, ()>::new();
        assert_eq!(*sf.call(1, || async { Ok(10) }).await.unwrap(), 10);
        assert_eq!(*sf.call(1, || async { Ok(20) }).await.unwrap(), 20);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn leader_cancellation_clears_entry() {
        let sf = Arc::new(SingleFlight::<u32, u32, ()>::new());
        let clone = Arc::clone(&sf);
        let task = tokio::spawn(async move {
            clone.call(7, || async {
                std::future::pending::<Result<u32, ()>>().await
            }).await
        });
        sleep(Duration::from_millis(5)).await;
        task.abort();
        let _ = task.await;
        assert_eq!(*sf.call(7, || async { Ok(9) }).await.unwrap(), 9);
    }
}
