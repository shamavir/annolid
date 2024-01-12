import os
import cv2
from pathlib import Path
from annolid.segmentation.SAM.segment_anything import SegmentAnythingModel
from annolid.data.videos import CV2Video
from annolid.utils.files import find_most_recent_file
import json
from annolid.gui.shape import Shape
from annolid.annotation.keypoints import save_labels
import numpy as np
from collections import deque, defaultdict
from shapely.geometry import Point, Polygon


def uniform_points_inside_polygon(polygon, num_points):
    # Get the bounding box of the polygon
    min_x, min_y, max_x, max_y = polygon.bounds

    # Generate random points within the bounding box
    random_points = np.column_stack((np.random.uniform(min_x, max_x, num_points),
                                     np.random.uniform(min_y, max_y, num_points)))

    # Filter points that are inside the polygon
    inside_points = [
        point for point in random_points if Point(point).within(polygon)]

    return np.array(inside_points)


def find_polygon_center(polygon_points):
    # Convert the list of polygon points to a Shapely Polygon
    polygon = Polygon(polygon_points)

    # Find the center of the polygon
    center = polygon.centroid

    return center


def random_sample_near_center(center, num_points, max_distance):
    # Randomly sample points near the center
    sampled_points = []
    for _ in range(num_points):
        # Generate random angle and radius
        angle = np.random.uniform(0, 2 * np.pi)
        radius = np.random.uniform(0, max_distance)

        # Calculate new point coordinates
        x = center.x + radius * np.cos(angle)
        y = center.y + radius * np.sin(angle)

        sampled_points.append((x, y))

    return np.array(sampled_points)


def random_sample_inside_edges(polygon, num_points):
    # Randomly sample points inside the edges of the polygon
    sampled_points = []
    min_x, min_y, max_x, max_y = polygon.bounds

    for _ in range(num_points):
        # Generate random point inside the bounding box
        x = np.random.uniform(min_x, max_x)
        y = np.random.uniform(min_y, max_y)
        point = Point(x, y)

        # Check if the point is inside the polygon
        if point.within(polygon):
            sampled_points.append((x, y))

    return np.array(sampled_points)


def random_sample_outside_edges(polygon, num_points):
    # Randomly sample points inside the edges of the polygon
    sampled_points = []
    min_x, min_y, max_x, max_y = polygon.bounds

    for _ in range(num_points):
        # Generate random point inside the bounding box
        x = np.random.uniform(min_x, max_x)
        y = np.random.uniform(min_y, max_y)
        point = Point(x, y)

        # Check if the point is inside the polygon
        if not point.within(polygon):
            sampled_points.append((x, y))

    return np.array(sampled_points)


def find_bbox(polygon_points):
    # Convert the list of polygon points to a NumPy array
    points_array = np.array(polygon_points)

    # Calculate the bounding box
    min_x, min_y = np.min(points_array, axis=0)
    max_x, max_y = np.max(points_array, axis=0)

    # Calculate the center point of the bounding box
    center_x = (min_x + max_x) / 2
    center_y = (min_y + max_y) / 2

    # Return the center point as a NumPy array
    bbox_center = np.array([(center_x, center_y)])

    return bbox_center


class MaxSizeQueue(deque):
    def __init__(self, max_size):
        super().__init__(maxlen=max_size)

    def enqueue(self, item):
        self.append(item)

    def to_numpy(self):
        return np.array(list(self))


def calculate_polygon_center(polygon_vertices):
    x_coords, y_coords = zip(*polygon_vertices)
    center_x = np.mean(x_coords)
    center_y = np.mean(y_coords)
    return np.array([(center_x, center_y)])


class VideoProcessor:
    """
    A class for processing video frames using the Segment-Anything model.
    """

    def __init__(self,
                 video_path,
                 num_center_points=3,
                 model_name="Segment-Anything (Edge)"
                 ):
        """
        Initialize the VideoProcessor.

        Parameters:
        - video_path (str): Path to the video file.
        - num_center_points (int): number of center points for prompt.
        """
        self.video_path = video_path
        self.video_folder = Path(video_path).with_suffix("")
        self.video_loader = CV2Video(video_path)
        self.sam_name = model_name
        self.edge_sam = self.get_model()
        self.num_frames = self.video_loader.total_frames()
        self.most_recent_file = self.get_most_recent_file()
        self.num_points_inside_edges = num_center_points
        self.num_center_points = num_center_points
        self.center_points_dict = defaultdict()

    def get_model(self,
                  encoder_path="edge_sam_3x_encoder.onnx",
                  decoder_path="edge_sam_3x_decoder.onnx"
                  ):
        """
        Load the Segment-Anything model.

        Parameters:
        - encoder_path (str): Path to the encoder model file.
        - decoder_path (str): Path to the decoder model file.
        - name (str): name of the SAM model

        Returns:
        - SegmentAnythingModel: The loaded model.
        """
        name = self.sam_name
        current_file_path = os.path.abspath(__file__)
        current_folder = os.path.dirname(current_file_path)
        encoder_path = os.path.join(current_folder, encoder_path)
        decoder_path = os.path.join(current_folder, decoder_path)
        model = SegmentAnythingModel(name, encoder_path, decoder_path)
        return model

    def load_json_file(self, json_file_path):
        """
        Load JSON file containing shapes and labels.

        Parameters:
        - json_file_path (str): Path to the JSON file.

        Returns:
        - tuple: A tuple containing two dictionaries (points_dict, point_labels_dict).
        """
        import labelme
        from annolid.annotation.masks import mask_to_polygons
        with open(json_file_path, 'r') as json_file:
            data = json.load(json_file)

        points_dict = {}
        point_labels_dict = {}

        for shape in data.get('shapes', []):
            label = shape.get('label')
            points = shape.get('points', [])
            mask = labelme.utils.img_b64_to_arr(
                shape["mask"]) if shape.get("mask") else None
            if mask is not None:
                polygons, has_holes = mask_to_polygons(mask)
                polys = polygons[0]
                points = np.array(
                    list(zip(polys[0::2], polys[1::2])))

            if label and points is not None:
                points_dict[label] = points
                point_labels_dict[label] = 1

        return points_dict, point_labels_dict

    def process_frame(self, frame_number):
        """
        Process a single frame of the video.

        Parameters:
        - frame_number (int): Frame number to process.
        """
        cur_frame = self.video_loader.load_frame(frame_number)
        self.edge_sam.set_image(cur_frame)
        filename = self.video_folder / \
            (self.video_folder.name + f"_{frame_number:0>{9}}.json")

        height, width, _ = cur_frame.shape
        if self.most_recent_file is None:
            return
        if (str(frame_number) not in str(self.most_recent_file) or
                str(frame_number - 1) not in str(self.most_recent_file)):
            last_frame_annotation = self.video_folder / \
                (self.video_folder.name + f"_{frame_number-1:0>{9}}.json")
            if os.path.exists(last_frame_annotation):
                self.most_recent_file = last_frame_annotation

        points_dict, _ = self.load_json_file(self.most_recent_file)
        label_list = []

        # Example usage of predict_polygon_from_points
        for label, points in points_dict.items():
            orig_points = points
            if len(points) == 0:
                continue
            if len(points) < 4:
                orig_points = random_sample_near_center(
                    Point(points[0]), 4, 3)
            points = calculate_polygon_center(orig_points)

            polygon = Polygon(orig_points)
            # Randomly sample points inside the edges of the polygon
            points_inside_edges = random_sample_inside_edges(polygon,
                                                             self.num_points_inside_edges)
            points_outside_edges = random_sample_outside_edges(polygon,
                                                               self.num_points_inside_edges * 3
                                                               )
            points_uni = uniform_points_inside_polygon(
                polygon, self.num_points_inside_edges)
            center_points = self.center_points_dict.get(label,
                                                        MaxSizeQueue(max_size=self.num_center_points))

            center_points.enqueue(points[0])
            points = center_points.to_numpy()
            self.center_points_dict[label] = center_points

            # use other instance's center points as negative point prompts
            other_polygon_center_points = [
                value for k, v in self.center_points_dict.items() if k != label for value in v]
            other_polygon_center_points = np.array(
                [(x[0], x[1]) for x in other_polygon_center_points])

            if len(points_inside_edges.shape) > 1:
                points = np.concatenate(
                    (points, points_inside_edges), axis=0)
            if len(points_uni) > 1:
                points = np.concatenate(
                    (points, points_uni), axis=0
                )

            point_labels = [1] * len(points)
            if len(points_outside_edges) > 1:
                points = np.concatenate(
                    (points, points_outside_edges), axis=0
                )
                point_labels += [0] * len(points_outside_edges)

            if len(other_polygon_center_points) > 1:
                points = np.concatenate(
                    (points, other_polygon_center_points),
                    axis=0
                )
                point_labels += [0] * len(other_polygon_center_points)

            polygon = self.edge_sam.predict_polygon_from_points(
                points, point_labels)

            # Save the LabelMe JSON to a file
            p_shape = Shape(label, shape_type='polygon', flags={})
            for x, y in polygon:
                # do not add 0,0 to the list
                if x >= 1 and y >= 1:
                    p_shape.addPoint((x, y))
            label_list.append(p_shape)

        self.most_recent_file = filename
        img_filename = str(filename.with_suffix('.png'))
        cur_frame = cv2.cvtColor(cur_frame, cv2.COLOR_BGR2RGB)
        if not Path(img_filename).exists():
            cv2.imwrite(img_filename, cur_frame)

        save_labels(filename=filename, imagePath=img_filename, label_list=label_list,
                    height=height, width=width)

    def process_video_frames(self, start_frame=0, end_frame=None, step=10):
        """
        Process multiple frames of the video.

        Parameters:
        - start_frame (int): Starting frame number.
        - end_frame (int): Ending frame number.
        - step (int): Step between frames.
        """
        if end_frame is None:
            end_frame = self.num_frames
        for i in range(start_frame, end_frame + 1, step):
            self.process_frame(i)

    def get_most_recent_file(self):
        """
        Find the most recent file in the video folder.

        Returns:
        - str: Path to the most recent file.
        """
        _recent_file = find_most_recent_file(self.video_folder)
        return _recent_file


if __name__ == '__main__':
    # Usage
    video_path = "squirrel.mp4"
    video_processor = VideoProcessor(video_path)
    video_processor.process_video_frames()