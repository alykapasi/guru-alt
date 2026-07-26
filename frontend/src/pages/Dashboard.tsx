import { ActivityCard } from "../components/dashboard/ActivityCard";
import { MasterySection } from "../components/dashboard/MasterySection";
import { ProfileSection } from "../components/dashboard/ProfileSection";
import { ReviewsDueCard } from "../components/dashboard/ReviewsDueCard";

export function Dashboard() {
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-h1">Dashboard</h1>
      <ActivityCard />
      <MasterySection />
      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        <ProfileSection />
        <ReviewsDueCard />
      </div>
    </div>
  );
}
